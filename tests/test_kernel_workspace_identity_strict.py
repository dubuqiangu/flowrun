"""域D2 · 工作区身份校验的 fail-closed 收紧（缺陷 1）。

旧实现的两种"跳过校验"都是 fail-open，可被手工写入 / 陈旧账本绕过，把**另一张图**
续跑上去：
(a) 账本非空但没有 flow_started 事件 → 直接 return；
(b) flow_started 存在但 planned_nodes 缺失或为空 → 只比对 run 名。
两者现都必须 fail-closed：拒绝执行（``FlowWorkspaceMismatchError`` → CLI 退 4），
诊断要说明原因与可执行建议，且仍然**零写入**。

边界：**空账本 / 账本不存在 = 全新工作区，仍必须放行**（不在本缺陷范围内）。

直跑：python -m unittest discover -s tests
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))

import kernel_test_support as support  # noqa: E402
from flowrun.kernel.engine import (  # noqa: E402
    FlowWorkspaceMismatchError,
    run_flow,
    verify_workspace_matches_flow,
)
from flowrun.state.ledger import EventLedger  # noqa: E402


class StrictWorkspaceIdentityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_identity_strict_")
        # 注册为 cleanup（LIFO）：账本 close 先执行，之后才删临时目录（Windows 文件锁）。
        self.addCleanup(self._temp_dir.cleanup)
        self.workspace = Path(self._temp_dir.name) / "workspace"
        self.stubs = support.KernelHookStubs()

    def open_ledger(self) -> EventLedger:
        ledger = EventLedger(self.workspace / "ledger.jsonl")
        self.addCleanup(ledger.close)
        return ledger

    def seed_ledger_with_events(self, *events) -> None:
        """按 (type, payload, idem, node) 播种账本（不落 flow_started，模拟异常账本）。"""
        seeded_ledger = EventLedger(self.workspace / "ledger.jsonl")
        try:
            for event_type, payload, idem_key, node_id in events:
                seeded_ledger.append(
                    event_type, payload, idem_key, support.DEFAULT_RUN_NAME, node_id
                )
        finally:
            seeded_ledger.close()

    # ---------------------------------------------------------------- (a) 缺 flow_started

    def test_nonempty_ledger_without_flow_started_is_rejected(self) -> None:
        """缺陷 1(a)：账本非空却没有 flow_started → 拒绝（旧实现直接跳过校验）。"""
        flow_ir = support.make_flow_ir([support.make_node("T001")], [])
        self.seed_ledger_with_events(
            ("node_done", {"artifacts": []}, f"{flow_ir.run_name}-T001-node-done-1", "T001"),
        )

        with self.assertRaises(FlowWorkspaceMismatchError) as raised_context:
            verify_workspace_matches_flow(flow_ir, self.open_ledger())

        message = str(raised_context.exception)
        self.assertIn("flow_started", message, "诊断须点明缺 flow_started")
        self.assertIn("换新工作区", message, "诊断须给出可执行建议")

    def test_nonempty_ledger_without_flow_started_is_rejected_zero_write(self) -> None:
        """缺陷 1(a) 零写入：拒绝路径不得落任何事件、不得新建工作区目录。"""
        standby_dir = self.workspace / "standby"
        standby_dir.mkdir(parents=True)
        flow_ir = support.make_flow_ir([support.make_node("T001")], [])
        open_ledger = EventLedger(standby_dir / "ledger.jsonl")
        open_ledger.append(
            "checkpoint", {"snapshot_path": "snap.json"}, "some-run-checkpoint-1", flow_ir.run_name
        )
        events_before = len(open_ledger.entries())

        with self.assertRaises(FlowWorkspaceMismatchError):
            verify_workspace_matches_flow(flow_ir, open_ledger)

        self.assertEqual(len(open_ledger.entries()), events_before, "校验不得落事件")
        self.assertEqual(
            sorted(child.name for child in standby_dir.iterdir()), ["ledger.jsonl"],
            "校验不得创建工作区目录",
        )
        open_ledger.close()

    def test_run_flow_rejects_ledger_without_flow_started(self) -> None:
        """缺陷 1(a)：run 与校验同口径——run_flow 也必须在任何推进前拒绝。"""
        flow_ir = support.make_flow_ir([support.make_node("T001")], [])
        self.seed_ledger_with_events(
            ("checkpoint", {"snapshot_path": "snap.json"},
             f"{flow_ir.run_name}-checkpoint-1", None),
        )

        with self.assertRaises(FlowWorkspaceMismatchError):
            asyncio.run(run_flow(flow_ir, self.workspace, self.stubs.build_hooks()))

        self.assertEqual(self.stubs.dispatch_calls, [], "身份不符时不得投递任何节点")
        self.assertEqual(
            len(self.open_ledger().entries()), 1, "拒绝路径不得追加任何事件"
        )

    # ---------------------------------------------------------------- (b) 缺 / 空 planned_nodes

    def test_flow_started_without_planned_nodes_is_rejected(self) -> None:
        """缺陷 1(b)：flow_started 缺 planned_nodes → 拒绝（旧实现只校验 run 名）。"""
        flow_ir = support.make_flow_ir([support.make_node("T001")], [])
        self.seed_ledger_with_events(
            ("flow_started", {"entry_nodes": ["T001"]},
             f"{flow_ir.run_name}-flow-started", None),
        )

        with self.assertRaises(FlowWorkspaceMismatchError) as raised_context:
            verify_workspace_matches_flow(flow_ir, self.open_ledger())

        message = str(raised_context.exception)
        self.assertIn("planned_nodes", message)
        self.assertIn("换新工作区", message)

    def test_flow_started_with_empty_planned_nodes_is_rejected(self) -> None:
        """缺陷 1(b)：planned_nodes 存在但为空 → 同样无法校验身份，拒绝。"""
        flow_ir = support.make_flow_ir([support.make_node("T001")], [])
        self.seed_ledger_with_events(
            ("flow_started", {"entry_nodes": ["T001"], "planned_nodes": []},
             f"{flow_ir.run_name}-flow-started", None),
        )

        with self.assertRaises(FlowWorkspaceMismatchError) as raised_context:
            verify_workspace_matches_flow(flow_ir, self.open_ledger())

        self.assertIn("planned_nodes", str(raised_context.exception))

    # ---------------------------------------------------------------- 严格集合相等（不回归）

    def test_equal_node_set_passes(self) -> None:
        """漏洞范围之外：planned_nodes 与当前图**严格集合相等**仍必须放行。"""
        flow_ir = support.make_flow_ir(
            [support.make_node("T001"), support.make_node("T002")],
            [support.make_edge("T001", "T002")],
        )
        self.seed_ledger_with_events(
            ("flow_started", {"entry_nodes": ["T001"], "planned_nodes": ["T001", "T002"]},
             f"{flow_ir.run_name}-flow-started", None),
        )

        verify_workspace_matches_flow(flow_ir, self.open_ledger())  # 不抛

    def test_node_superset_is_rejected(self) -> None:
        """多一个节点（新图是超集）→ 仍拒绝（不回归）。"""
        flow_ir = support.make_flow_ir(
            [support.make_node("T001"), support.make_node("T002")], []
        )
        self.seed_ledger_with_events(
            ("flow_started", {"entry_nodes": ["T001"], "planned_nodes": ["T001"]},
             f"{flow_ir.run_name}-flow-started", None),
        )

        with self.assertRaises(FlowWorkspaceMismatchError):
            verify_workspace_matches_flow(flow_ir, self.open_ledger())

    def test_node_subset_is_rejected(self) -> None:
        """少一个节点（工作区计划里的节点不在当前图）→ 仍拒绝（不回归）。"""
        flow_ir = support.make_flow_ir([support.make_node("T001")], [])
        self.seed_ledger_with_events(
            ("flow_started", {"entry_nodes": ["T001"], "planned_nodes": ["T001", "T002"]},
             f"{flow_ir.run_name}-flow-started", None),
        )

        with self.assertRaises(FlowWorkspaceMismatchError) as raised_context:
            verify_workspace_matches_flow(flow_ir, self.open_ledger())

        self.assertIn("T002", str(raised_context.exception))

    # ---------------------------------------------------------------- 新工作区仍放行

    def test_absent_ledger_is_not_rejected(self) -> None:
        """账本不存在 = 全新工作区：verify 放行（run_flow 随后落 flow_started）。"""
        flow_ir = support.make_flow_ir([support.make_node("T001")], [])
        verify_workspace_matches_flow(flow_ir, self.open_ledger())  # 不抛

        final_state = asyncio.run(
            run_flow(flow_ir, self.workspace, self.stubs.build_hooks())
        )
        self.assertTrue(final_state.is_done())
        started_events = support.events_of_type(self.open_ledger(), "flow_started")
        self.assertEqual(len(started_events), 1)

    def test_empty_ledger_is_not_rejected(self) -> None:
        """空账本（0 字节）= 全新工作区：必须放行，不得被缺陷 1 的收紧误伤。"""
        flow_ir = support.make_flow_ir([support.make_node("T001")], [])
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "ledger.jsonl").write_bytes(b"")

        verify_workspace_matches_flow(flow_ir, self.open_ledger())  # 不抛


if __name__ == "__main__":
    unittest.main()
