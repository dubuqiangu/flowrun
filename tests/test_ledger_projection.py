"""域B · FlowState 投影测试（tasks.md T004）：事件剧本如何投影成节点状态。

直跑：python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import ledger_test_support

from ledger_test_support import (  # noqa: E402  (负责把 src/ 加进 sys.path)
    RUN_NAME,
    LedgerTestCase,
    append_sample_events,
)
from flowrun.state.flow_state import STATUS_UNKNOWN  # noqa: E402
from flowrun.state.ledger import EventLedger  # noqa: E402


class TestFlowStateProjection(LedgerTestCase):
    def test_flow_started_projects_entry_and_planned_nodes(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 1)
        flow_state = ledger.replay()
        self.assertEqual(flow_state.node_status("T001"), "ready")
        self.assertEqual(flow_state.node_status("T002"), "planned")
        self.assertEqual(flow_state.node_status("H001"), "planned")
        self.assertEqual(flow_state.node_status("T999"), STATUS_UNKNOWN)

    def test_node_lifecycle_progresses_to_done(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 7)
        flow_state = ledger.replay()
        self.assertEqual(flow_state.node_status("T001"), "done")  # node_done 最后落账
        self.assertEqual(flow_state.node_status("T002"), "ready")  # edge_taken 的 target
        self.assertEqual(flow_state.completed_nodes(), {"T001"})
        self.assertEqual(flow_state.hop_count(), 1)  # 一次 agent_dispatched

    def test_agent_exited_and_checkpoint_are_state_neutral(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 3)  # 到 agent_dispatched
        ledger.append("agent_exited", {"exit_code": 0}, "T001-exit-1", RUN_NAME, "T001")
        ledger.append("checkpoint", {"snapshot_path": "snap/1.json"}, "checkpoint-1", RUN_NAME)
        flow_state = ledger.replay()
        self.assertEqual(flow_state.node_status("T001"), "running")  # 等 verdict 才转移
        self.assertEqual(flow_state.token_spent(), 0)

    def test_verdict_moves_node_to_judging(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 5)
        self.assertEqual(ledger.replay().node_status("T001"), "judging")

    def test_human_flow_pending_and_resolution(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 15)  # H001 → awaiting_human
        flow_state = ledger.replay()
        self.assertEqual(flow_state.pending_human_nodes(), {"H001"})
        ledger.append("human_resolved", {"decision": "approve"}, "H001-resolve-1", RUN_NAME, "H001")
        flow_state = ledger.replay()
        self.assertEqual(flow_state.node_status("H001"), "done")
        self.assertEqual(flow_state.pending_human_nodes(), set())

    def test_human_resolved_deny_fails_and_unknown_decision_keeps_awaiting(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 15)
        ledger.append("human_resolved", {"decision": "deny"}, "H001-resolve-deny", RUN_NAME, "H001")
        self.assertEqual(ledger.replay().node_status("H001"), "failed")

        odd_decision_ledger = EventLedger(self.ledger_dir / "odd_decision_run.jsonl")  # 独立文件
        self.addCleanup(odd_decision_ledger.close)
        append_sample_events(odd_decision_ledger, 15)
        odd_decision_ledger.append("human_resolved", {"decision": "maybe"}, "H001-resolve-odd",
                                   RUN_NAME, "H001")
        self.assertEqual(odd_decision_ledger.replay().node_status("H001"), "awaiting_human")

    def test_gate_tripped_pauses_and_records_gate(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 3)
        ledger.append("gate_tripped", {"gate": "token_budget", "actual": 1100000, "limit": 1000000},
                      "gate-token-1", RUN_NAME)
        flow_state = ledger.replay()
        self.assertTrue(flow_state.is_paused())
        self.assertEqual(flow_state.tripped_gates(), ("token_budget",))
        ledger.append("flow_resumed", {}, "resume-1", RUN_NAME)
        self.assertFalse(ledger.replay().is_paused())

    def test_user_directive_pause_marks_node_and_resume_restores(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 2)  # T001 停在 ready
        ledger.append("user_directive", {"directive": "pause"}, "pause-T001", RUN_NAME, "T001")
        flow_state = ledger.replay()
        self.assertEqual(flow_state.node_status("T001"), "paused")
        self.assertTrue(flow_state.is_paused())
        ledger.append("flow_resumed", {}, "resume-1", RUN_NAME)
        resumed_flow_state = ledger.replay()
        self.assertEqual(resumed_flow_state.node_status("T001"), "ready")  # 恢复暂停前状态
        self.assertFalse(resumed_flow_state.is_paused())

    def test_user_directive_pause_without_node_sets_global_pause_only(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 2)
        ledger.append("user_directive", {"directive": "pause"}, "pause-global", RUN_NAME)
        flow_state = ledger.replay()
        self.assertTrue(flow_state.is_paused())
        self.assertEqual(flow_state.node_status("T001"), "ready")  # 节点不受牵连

    def test_user_directive_skip_marks_node_skipped(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 2)
        ledger.append("user_directive", {"directive": "skip"}, "skip-T002", RUN_NAME, "T002")
        self.assertEqual(ledger.replay().node_status("T002"), "skipped")

    def test_user_directive_never_overrides_terminal_status(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 7)  # T001 done
        ledger.append("user_directive", {"directive": "skip"}, "skip-late-T001", RUN_NAME, "T001")
        ledger.append("user_directive", {"directive": "pause"}, "pause-late-T001", RUN_NAME, "T001")
        flow_state = ledger.replay()
        self.assertEqual(flow_state.node_status("T001"), "done")
        self.assertTrue(flow_state.is_paused())  # 全局暂停位仍生效

    def test_flow_halted_and_flow_done_terminal_flags(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 3)
        ledger.append("flow_halted", {}, "halt-1", RUN_NAME)
        halted_state = ledger.replay()
        self.assertTrue(halted_state.is_halted())
        self.assertFalse(halted_state.is_done())
        ledger.append("flow_done", {}, "done-1", RUN_NAME)
        final_state = ledger.replay()
        self.assertTrue(final_state.is_done())
        self.assertTrue(final_state.is_halted())  # 已发生的事实不被覆盖

    def test_token_accumulation_from_multiple_event_types(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 13)  # 1200 + 800 + 300 + 250
        self.assertEqual(ledger.replay().token_spent(), 2550)

    def test_unknown_event_type_is_ignored(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 2)
        ledger.append("future_event_kind", {"anything": True}, "future-1", RUN_NAME, "T001")
        flow_state = ledger.replay()
        self.assertEqual(flow_state.node_status("T001"), "ready")  # 前向兼容：不崩、不改状态


if __name__ == "__main__":
    unittest.main()
