"""域D2 · 工作区与流程身份校验测试（FIX-B3）：flowrun run b.mmd --workspace a的ws
不得恢复出 A 的状态后宣告 done，必须抛 FlowWorkspaceMismatchError（cli 捕获退 4）。

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


class WorkspaceIdentityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_identity_test_")
        # 注册为 cleanup（LIFO）：测试期间注册的账本 close 先执行，之后才删临时目录；
        # 若在 tearDown 里删目录，Windows 上仍打开的 ledger.jsonl 会让 rmtree 失败。
        self.addCleanup(self._temp_dir.cleanup)
        self.workspace = Path(self._temp_dir.name) / "workspace"
        self.stubs = support.KernelHookStubs()

    def run_flow_once(self, flow_ir) -> object:
        return asyncio.run(run_flow(flow_ir, self.workspace, self.stubs.build_hooks()))

    def reopened_ledger(self) -> EventLedger:
        """重开账本读取落账结果；注册清理，避免测试结束后遗留写句柄（ResourceWarning）。"""
        ledger = EventLedger(self.workspace / "ledger.jsonl")
        self.addCleanup(ledger.close)
        return ledger

    # ---------------------------------------------------------------- 身份不匹配

    def test_other_flow_on_same_workspace_raises_mismatch(self) -> None:
        """FIX-B3 复现：A 图跑完的工作区再跑 B 图 → 抛 FlowWorkspaceMismatchError，
        B 图整图不得执行（修复前会恢复出 A 的 flow_done 直接宣告完成）。"""
        flow_a = support.make_flow_ir(
            [support.make_node("A001")], [], run_name="release-login-a"
        )
        flow_b = support.make_flow_ir(
            [support.make_node("B001")], [], run_name="release-login-b"
        )
        self.assertTrue(self.run_flow_once(flow_a).is_done())

        with self.assertRaises(FlowWorkspaceMismatchError) as raised_context:
            self.run_flow_once(flow_b)

        self.assertIn("release-login-a", str(raised_context.exception))
        self.assertIn("release-login-b", str(raised_context.exception))
        ledger = self.reopened_ledger()
        self.assertEqual(
            support.events_of_type(ledger, "agent_dispatched", "B001"), [],
            "身份不匹配时 B 图不得投递任何节点",
        )

    def test_same_run_name_but_foreign_node_set_raises_mismatch(self) -> None:
        """FIX-B3：run 名相同但工作区计划节点已不在当前图中（图被替换）→ 同样拒绝。"""
        original_flow = support.make_flow_ir(
            [support.make_node("A001"), support.make_node("B001")],
            [support.make_edge("A001", "B001")],
        )
        shrunk_flow = support.make_flow_ir([support.make_node("A001")], [])
        self.assertTrue(self.run_flow_once(original_flow).is_done())

        with self.assertRaises(FlowWorkspaceMismatchError):
            self.run_flow_once(shrunk_flow)

    # ---------------------------------------------------------------- 身份一致

    def test_same_flow_rerun_on_own_workspace_is_unaffected(self) -> None:
        """身份一致时续跑不被拦：第二次 run 幂等（零新事件、仍 done）。"""
        flow_ir = support.make_flow_ir([support.make_node("T001")], [])
        self.assertTrue(self.run_flow_once(flow_ir).is_done())
        events_after_first_run = len(self.reopened_ledger().entries())

        second_state = self.run_flow_once(flow_ir)

        self.assertTrue(second_state.is_done())
        self.assertEqual(len(self.reopened_ledger().entries()), events_after_first_run)

    def test_empty_workspace_is_not_rejected(self) -> None:
        """全新工作区（空账本）无需身份校验：首次 run 正常落 flow_started。"""
        flow_ir = support.make_flow_ir([support.make_node("T001")], [])
        final_state = self.run_flow_once(flow_ir)

        self.assertTrue(final_state.is_done())
        started_events = support.events_of_type(self.reopened_ledger(), "flow_started")
        self.assertEqual(len(started_events), 1)
        self.assertEqual(started_events[0].payload["planned_nodes"], ["T001"])

    # ---------------------------------------------------------------- K2 严格集合相等

    def test_node_superset_flow_on_same_run_name_raises_mismatch(self) -> None:
        """K2 复现：run 名相同、新图是旧计划的**超集** → 必须拒绝。

        修复前只拦"节点消失"，超集被放行；叠加"已 done 的 run 直接返回"，追加的
        节点会被静默继承旧 run 的 flow_done，永不执行。"""
        flow_a = support.make_flow_ir(
            [support.make_node("A001")], [], run_name="release-login-a"
        )
        flow_b = support.make_flow_ir(
            [support.make_node("A001"), support.make_node("C001")],
            [],
            run_name="release-login-a",
        )
        self.assertTrue(self.run_flow_once(flow_a).is_done())

        with self.assertRaises(FlowWorkspaceMismatchError) as raised_context:
            self.run_flow_once(flow_b)

        self.assertIn("C001", str(raised_context.exception))
        self.assertEqual(
            support.events_of_type(self.reopened_ledger(), "agent_dispatched", "C001"),
            [],
            "拒绝后追加节点不得被投递",
        )

    def test_node_subset_flow_on_same_run_name_raises_mismatch(self) -> None:
        """K2：工作区计划节点在当前图中消失 → 拒绝，且消息指明"图已被替换"。"""
        original_flow = support.make_flow_ir(
            [support.make_node("A001"), support.make_node("B001")], []
        )
        shrunk_flow = support.make_flow_ir([support.make_node("A001")], [])
        self.assertTrue(self.run_flow_once(original_flow).is_done())

        with self.assertRaises(FlowWorkspaceMismatchError) as raised_context:
            self.run_flow_once(shrunk_flow)

        self.assertIn("B001", str(raised_context.exception))

    def test_equal_node_set_is_allowed(self) -> None:
        """K2：planned_nodes 与当前图节点集双向一致 → 放行（续跑幂等）。"""
        flow_ir = support.make_flow_ir(
            [support.make_node("T001"), support.make_node("T002")],
            [support.make_edge("T001", "T002")],
        )
        self.assertTrue(self.run_flow_once(flow_ir).is_done())
        events_after_first_run = len(self.reopened_ledger().entries())

        second_state = self.run_flow_once(flow_ir)

        self.assertTrue(second_state.is_done())
        self.assertEqual(len(self.reopened_ledger().entries()), events_after_first_run)

    def test_missing_planned_nodes_is_rejected(self) -> None:
        """缺陷 1（fail-closed）：老账本 / 手写夹具 payload 缺 planned_nodes → 拒绝。

        旧行为"只校验 run 名"是 fail-open：无计划节点集可比时身份无从校验，
        同名但另一张图的流程可以绕过身份校验续跑上去。现改为"无法校验即拒绝"，
        诊断须指明缺 planned_nodes 并建议换新工作区。

        说明：这是**纠正前置条件**（把"跳过校验"改成"拒绝"），不是放宽断言——
        被纠正的正是旧断言所钉住的宽松行为。"""
        flow_ir = support.make_flow_ir([support.make_node("T001")], [])
        seeded_ledger = EventLedger(self.workspace / "ledger.jsonl")
        seeded_ledger.append(
            "flow_started",
            {"entry_nodes": ["T001"]},  # 故意不带 planned_nodes
            f"{flow_ir.run_name}-flow-started",
            flow_ir.run_name,
        )
        seeded_ledger.close()

        with self.assertRaises(FlowWorkspaceMismatchError) as raised_context:
            verify_workspace_matches_flow(flow_ir, self.reopened_ledger())
        self.assertIn("planned_nodes", str(raised_context.exception))
        self.assertIn("换新工作区", str(raised_context.exception))

        foreign_run_flow = support.make_flow_ir(
            [support.make_node("T001")], [], run_name="other-release-flow"
        )
        with self.assertRaises(FlowWorkspaceMismatchError):
            verify_workspace_matches_flow(foreign_run_flow, self.reopened_ledger())

    def test_verification_writes_nothing_to_ledger(self) -> None:
        """K2：校验是零写入纯查询——放行与拒绝两条路径都不得落事件 / 建目录。"""
        standby_dir = self.workspace / "standby"
        standby_dir.mkdir(parents=True)
        flow_ir = support.make_flow_ir([support.make_node("T001")], [])
        open_ledger = EventLedger(standby_dir / "ledger.jsonl")
        open_ledger.append(
            "flow_started",
            {"entry_nodes": ["T001"], "planned_nodes": ["T001"]},
            f"{flow_ir.run_name}-flow-started",
            flow_ir.run_name,
        )
        events_before = len(open_ledger.entries())

        verify_workspace_matches_flow(flow_ir, open_ledger)  # 已打开的 EventLedger 上直接调用

        superset_flow = support.make_flow_ir(
            [support.make_node("T001"), support.make_node("T002")], []
        )
        with self.assertRaises(FlowWorkspaceMismatchError):
            verify_workspace_matches_flow(superset_flow, open_ledger)

        self.assertEqual(len(open_ledger.entries()), events_before, "校验不得落任何事件")
        self.assertEqual(
            sorted(child.name for child in standby_dir.iterdir()), ["ledger.jsonl"],
            "校验不得创建工作区目录",
        )
        open_ledger.close()  # Windows：句柄未释放会挡住临时目录清理


if __name__ == "__main__":
    unittest.main()
