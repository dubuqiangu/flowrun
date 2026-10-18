"""内核摘要的可诊断段：join 阻塞诊断（T020k）+ 失败归因投影（T020j）。

直跑：python -m unittest discover -s tests

背景：join 的前驱全部不可达时，流程只会**静默**停在 in-progress——"前驱还在正常
推进（真等）"与"前驱已 failed（永远不会到达，异常挂起）"在账本里没有区别信号。
本轮用**既有状态投影**（不新增事件类型）把两者在摘要里分辨出来，并把 node_failed
的 payload 原样投影成失败原因行（免去人工回到相邻事件反推）。
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import kernel_test_support

import kernel_test_support as support  # noqa: E402  (负责把 src/ 加进 sys.path)
from flowrun.ir.schema import NODE_KIND_JOIN  # noqa: E402
from flowrun.kernel.state_summary import (  # noqa: E402
    describe_join_blocking,
    describe_node_failures,
    print_state_summary,
)
from flowrun.state.flow_state import FlowState  # noqa: E402
from flowrun.state.ledger import EventLedger  # noqa: E402

LEFT_PREDECESSOR = "T001"
RIGHT_PREDECESSOR = "T002"
JOIN_NODE = "J001"


class StateSummaryDiagnosisTest(unittest.TestCase):
    """摘要诊断段：只读投影，必须能区分 join 正常等待 vs 异常挂起。"""

    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_summary_test_")
        self.addCleanup(temp_dir.cleanup)
        self.ledger_path = Path(temp_dir.name) / "ledger.jsonl"

    def make_join_flow_ir(self):
        """T001 / T002 两条 barrier 边汇入 J001（join）。"""
        return support.make_flow_ir(
            nodes=[
                support.make_node(LEFT_PREDECESSOR, agent=support.DEFAULT_AGENT_NAME),
                support.make_node(RIGHT_PREDECESSOR, agent=support.DEFAULT_AGENT_NAME),
                support.make_node(JOIN_NODE, kind=NODE_KIND_JOIN),
            ],
            edges=[
                support.make_edge(LEFT_PREDECESSOR, JOIN_NODE, when="all"),
                support.make_edge(RIGHT_PREDECESSOR, JOIN_NODE, when="all"),
            ],
        )

    def open_projected_ledger(self) -> tuple[FlowState, EventLedger]:
        """打开账本并投影出 FlowState（调用方自行 append 事件后再取状态）。"""
        ledger = EventLedger(self.ledger_path)
        self.addCleanup(ledger.close)
        return FlowState(), ledger

    def append_flow_started(self, ledger: EventLedger) -> None:
        ledger.append(
            "flow_started",
            {"entry_nodes": [LEFT_PREDECESSOR], "planned_nodes": [RIGHT_PREDECESSOR]},
            "summary-flow-started",
            support.DEFAULT_RUN_NAME,
            LEFT_PREDECESSOR,
        )

    def capture_summary(self, flow_ir, flow_state, ledger=None) -> str:
        stdout_capture = io.StringIO()
        with contextlib.redirect_stdout(stdout_capture):
            print_state_summary(flow_ir, flow_state, title="summary", ledger=ledger)
        return stdout_capture.getvalue()

    # ------------------------------------------------------- join 阻塞诊断（T020k）

    def test_join_blocked_by_failed_predecessor_is_reported_as_abnormal_hang(self) -> None:
        """前驱已 failed：barrier 只认 done/skipped，该前驱永远不会到达 → 必须明说。"""
        flow_state, ledger = self.open_projected_ledger()
        self.append_flow_started(ledger)
        ledger.append(
            "node_failed",
            {"exit_code": 3, "timed_out": False, "reason": "nonzero_exit"},
            "summary-T002-failed",
            support.DEFAULT_RUN_NAME,
            RIGHT_PREDECESSOR,
        )
        flow_state.merge_from_events(ledger.entries())

        blocking_lines = describe_join_blocking(self.make_join_flow_ir(), flow_state)

        self.assertEqual(len(blocking_lines), 1)
        self.assertIn("join 阻塞", blocking_lines[0])
        self.assertIn(JOIN_NODE, blocking_lines[0])
        self.assertIn(RIGHT_PREDECESSOR, blocking_lines[0])
        self.assertIn("failed", blocking_lines[0])

    def test_join_with_progressing_predecessors_is_reported_as_normal_wait(self) -> None:
        """无前驱 failed：只是还没到齐 → 报"正常等待"，不得伪装成异常挂起。"""
        flow_state, ledger = self.open_projected_ledger()
        self.append_flow_started(ledger)
        flow_state.merge_from_events(ledger.entries())

        blocking_lines = describe_join_blocking(self.make_join_flow_ir(), flow_state)

        self.assertEqual(len(blocking_lines), 1)
        self.assertIn("join 等待", blocking_lines[0])
        self.assertIn(RIGHT_PREDECESSOR, blocking_lines[0])
        self.assertNotIn("阻塞", blocking_lines[0])

    def test_resolved_join_is_not_reported(self) -> None:
        """join 已收口（done）后不得再报等待/阻塞（避免噪声）。"""
        flow_state, ledger = self.open_projected_ledger()
        self.append_flow_started(ledger)
        ledger.append(
            "node_done", {"artifacts": []}, "summary-J001-done",
            support.DEFAULT_RUN_NAME, JOIN_NODE,
        )
        flow_state.merge_from_events(ledger.entries())

        self.assertEqual(describe_join_blocking(self.make_join_flow_ir(), flow_state), [])

    # --------------------------------------------------- 失败归因投影（T020j）

    def test_failure_reason_is_projected_from_payload(self) -> None:
        flow_state, ledger = self.open_projected_ledger()
        self.append_flow_started(ledger)
        ledger.append(
            "node_failed",
            {"exit_code": 3, "timed_out": False, "reason": "nonzero_exit"},
            "summary-T002-failed",
            support.DEFAULT_RUN_NAME,
            RIGHT_PREDECESSOR,
        )
        flow_state.merge_from_events(ledger.entries())

        failure_lines = describe_node_failures(
            self.make_join_flow_ir(), flow_state, ledger
        )

        self.assertEqual(len(failure_lines), 1)
        self.assertIn(RIGHT_PREDECESSOR, failure_lines[0])
        self.assertIn("reason=nonzero_exit", failure_lines[0])
        self.assertIn("exit_code=3", failure_lines[0])

    def test_failure_lines_require_a_ledger(self) -> None:
        """不给账本就不投影失败原因（摘要默认输出与拆出前完全一致）。"""
        flow_state, ledger = self.open_projected_ledger()
        self.append_flow_started(ledger)
        ledger.append(
            "node_failed",
            {"exit_code": 3, "timed_out": False, "reason": "nonzero_exit"},
            "summary-T002-failed",
            support.DEFAULT_RUN_NAME,
            RIGHT_PREDECESSOR,
        )
        flow_state.merge_from_events(ledger.entries())

        self.assertEqual(
            describe_node_failures(self.make_join_flow_ir(), flow_state, None), []
        )
        summary_without_ledger = self.capture_summary(
            self.make_join_flow_ir(), flow_state
        )
        self.assertNotIn("失败:", summary_without_ledger)
        self.assertIn(
            "失败:",
            self.capture_summary(self.make_join_flow_ir(), flow_state, ledger),
        )

    def test_summary_without_flow_ir_skips_diagnosis(self) -> None:
        """status 不带 --flow 时没有图：诊断段必须整体跳过而不是崩掉。"""
        flow_state, ledger = self.open_projected_ledger()
        self.append_flow_started(ledger)
        flow_state.merge_from_events(ledger.entries())

        self.assertEqual(describe_join_blocking(None, flow_state), [])
        self.assertEqual(describe_node_failures(None, flow_state, ledger), [])
        self.assertIn("flow: in-progress", self.capture_summary(None, flow_state))


if __name__ == "__main__":
    unittest.main()
