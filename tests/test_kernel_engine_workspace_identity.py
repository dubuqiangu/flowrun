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
from flowrun.kernel.engine import FlowWorkspaceMismatchError, run_flow  # noqa: E402
from flowrun.state.ledger import EventLedger  # noqa: E402


class WorkspaceIdentityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_identity_test_")
        self.workspace = Path(self._temp_dir.name) / "workspace"
        self.stubs = support.KernelHookStubs()

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def run_flow_once(self, flow_ir) -> object:
        return asyncio.run(run_flow(flow_ir, self.workspace, self.stubs.build_hooks()))

    def reopened_ledger(self) -> EventLedger:
        return EventLedger(self.workspace / "ledger.jsonl")

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


if __name__ == "__main__":
    unittest.main()
