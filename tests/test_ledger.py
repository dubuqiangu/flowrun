"""域B · ledger 测试（tasks.md T004）：追加/哈希链/幂等重放/崩溃恢复/FlowState 投影。

直跑：python -m unittest discover -s tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from flowrun.state import ledger as ledger_module  # noqa: E402
from flowrun.state.flow_state import STATUS_UNKNOWN  # noqa: E402
from flowrun.state.ledger import (  # noqa: E402
    GENESIS_PREV_HASH,
    EventLedger,
    canonical_json,
    compute_entry_hash,
)

RUN_NAME = "release-login"

# 一段可重放的自洽事件剧本（节点 T001 → T002 → 人审 H001）
SAMPLE_EVENTS: list[tuple[str, dict, str, str | None]] = [
    ("flow_started", {"entry_nodes": ["T001"], "planned_nodes": ["T002", "H001"]}, "run-start", None),
    ("node_entered", {}, "T001-enter-1", "T001"),
    ("agent_dispatched", {}, "T001-dispatch-1", "T001"),
    ("agent_exited", {"exit_code": 0, "tokens": 1200}, "T001-exit-1", "T001"),
    ("verdict", {"edge": "ok", "confidence": 0.9}, "T001-verdict-1", "T001"),
    ("edge_taken", {"target": "T002", "edge": "ok"}, "T001-edge-1", "T001"),
    ("node_done", {"tokens": 300}, "T001-done-1", "T001"),
    ("node_entered", {}, "T002-enter-1", "T002"),
    ("agent_dispatched", {}, "T002-dispatch-1", "T002"),
    ("agent_exited", {"exit_code": 0, "tokens": 800}, "T002-exit-1", "T002"),
    ("verdict", {"edge": "fail", "confidence": 0.85}, "T002-verdict-1", "T002"),
    ("edge_taken", {"target": "H001", "edge": "fail"}, "T002-edge-1", "T002"),
    ("node_done", {"tokens": 250}, "T002-done-1", "T002"),
    ("node_entered", {}, "H001-enter-1", "H001"),
    ("human_requested", {}, "H001-request-1", "H001"),
    ("human_resolved", {"decision": "approve"}, "H001-resolve-1", "H001"),
]


def append_sample_events(
    ledger: EventLedger, event_count: int, first_event_index: int = 0
) -> None:
    """按剧本顺序投递事件；first_event_index 用于崩溃续跑场景从中间续投。"""
    for event_type, payload, idem_key, node_id in SAMPLE_EVENTS[
        first_event_index : first_event_index + event_count
    ]:
        ledger.append(event_type, payload, idem_key, RUN_NAME, node_id)


class LedgerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_ledger_test_")
        self.addCleanup(temp_dir.cleanup)
        self.ledger_dir = Path(temp_dir.name)
        self.ledger_path = self.ledger_dir / "ledger.jsonl"

    def make_ledger(self) -> EventLedger:
        ledger = EventLedger(self.ledger_path)
        self.addCleanup(ledger.close)
        return ledger

    def rewrite_line(self, line_index: int, mutated_record: dict) -> None:
        """按行号（0 起）替换磁盘上的一行（模拟篡改，不重算哈希链）。"""
        raw_lines = self.ledger_path.read_bytes().split(b"\n")
        raw_lines[line_index] = canonical_json(mutated_record).encode("utf-8")
        self.ledger_path.write_bytes(b"\n".join(raw_lines))


class TestLedgerAppend(LedgerTestCase):
    def test_append_assigns_incrementing_seq_and_links_chain(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 3)
        entries = ledger.entries()
        self.assertEqual([entry.seq for entry in entries], [1, 2, 3])
        self.assertEqual(entries[0].prev_hash, GENESIS_PREV_HASH)
        self.assertEqual(entries[1].prev_hash, compute_entry_hash(entries[0]))
        self.assertEqual(entries[2].prev_hash, compute_entry_hash(entries[1]))

    def test_written_lines_are_canonical_json(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 2)
        raw_lines = self.ledger_path.read_bytes().split(b"\n")
        self.assertEqual(raw_lines[-1], b"")  # 以换行符收束，不留半行
        for raw_line in raw_lines[:-1]:
            parsed_record = json.loads(raw_line)
            self.assertEqual(canonical_json(parsed_record).encode("utf-8"), raw_line)

    def test_append_is_immediately_visible_to_new_instance(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 2)
        ledger.close()  # flush + fsync 之后无需依赖句柄
        reloaded_ledger = EventLedger(self.ledger_path)
        self.addCleanup(reloaded_ledger.close)
        self.assertEqual(len(reloaded_ledger.entries()), 2)
        self.assertEqual(reloaded_ledger.last_seq, 2)

    def test_append_rejects_invalid_arguments(self) -> None:
        ledger = self.make_ledger()
        with self.assertRaises(ValueError):
            ledger.append("node_done", {}, "", RUN_NAME, "T001")  # 空 idem
        with self.assertRaises(TypeError):
            ledger.append("node_done", ["not", "a", "dict"], "idem-1", RUN_NAME)  # payload 非 dict
        with self.assertRaises(ValueError):
            ledger.append("node_done", {}, "idem-2", "")  # 空 run_name


class TestChainVerification(LedgerTestCase):
    def test_verify_chain_true_on_intact_ledger(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 16)
        self.assertTrue(ledger.verify_chain())

    def test_tampered_payload_detected(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 5)
        tampered_record = ledger.entries()[2].to_dict()
        tampered_record["payload"]["tokens"] = 999999  # 改中间一行的内容，不动 prev_hash
        self.rewrite_line(2, tampered_record)
        self.assertFalse(ledger.verify_chain())

    def test_tampered_prev_hash_detected(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 5)
        tampered_record = ledger.entries()[3].to_dict()
        tampered_record["prev_hash"] = "f" * 64
        self.rewrite_line(3, tampered_record)
        self.assertFalse(ledger.verify_chain())

    def test_tampered_seq_detected(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 5)
        tampered_record = ledger.entries()[2].to_dict()
        tampered_record["seq"] = 99
        self.rewrite_line(2, tampered_record)
        self.assertFalse(ledger.verify_chain())

    def test_corrupt_middle_line_detected(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 4)
        raw_lines = self.ledger_path.read_bytes().split(b"\n")
        raw_lines[1] = b"<<< not json >>>"
        self.ledger_path.write_bytes(b"\n".join(raw_lines))
        self.assertFalse(ledger.verify_chain())

    def test_incomplete_tail_ignored_by_verify(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 3)
        with open(self.ledger_path, "ab") as append_handle:
            append_handle.write(b'{"seq":4,"prev_h')  # 模拟崩溃写了一半的行（无换行符）
        self.assertTrue(ledger.verify_chain())


class TestIdemDedupReplay(LedgerTestCase):
    def test_duplicate_idem_projection_takes_first(self) -> None:
        ledger = self.make_ledger()
        ledger.append("node_entered", {}, "T001-enter-1", RUN_NAME, "T001")
        # 相同 idem 重复投递且内容更"靠后"：重放只认第一条
        ledger.append("node_done", {"tokens": 500}, "T001-enter-1", RUN_NAME, "T001")
        flow_state = ledger.replay()
        self.assertEqual(flow_state.node_status("T001"), "ready")

    def test_duplicate_idem_does_not_double_count_tokens_or_hops(self) -> None:
        ledger = self.make_ledger()
        ledger.append("agent_dispatched", {"tokens": 100}, "T001-dispatch-1", RUN_NAME, "T001")
        ledger.append("agent_dispatched", {"tokens": 999}, "T001-dispatch-1", RUN_NAME, "T001")
        flow_state = ledger.replay()
        self.assertEqual(flow_state.hop_count(), 1)
        self.assertEqual(flow_state.token_spent(), 100)

    def test_out_of_order_duplicate_delivery_replays_consistently(self) -> None:
        canonical_ledger = self.make_ledger()
        append_sample_events(canonical_ledger, 3)

        duplicated_ledger = self.make_ledger()
        duplicated_ledger.append("flow_started", SAMPLE_EVENTS[0][1], "run-start", RUN_NAME)
        duplicated_ledger.append("node_entered", {}, "T001-enter-1", RUN_NAME, "T001")
        duplicated_ledger.append("node_done", {"tokens": 7777}, "run-start", RUN_NAME, None)  # 乱序重复
        duplicated_ledger.append("agent_dispatched", {}, "T001-dispatch-1", RUN_NAME, "T001")
        duplicated_ledger.append("node_entered", {}, "T001-enter-1", RUN_NAME, "T001")  # 重复
        duplicated_ledger.append("agent_dispatched", {"tokens": 9}, "T001-dispatch-1", RUN_NAME, "T001")  # 重复

        self.assertEqual(duplicated_ledger.replay(), canonical_ledger.replay())


class TestCrashRecovery(LedgerTestCase):
    def test_partial_tail_line_is_dropped_on_reload(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 3)
        clean_flow_state = ledger.replay()
        with open(self.ledger_path, "ab") as append_handle:
            append_handle.write(b'{"seq":4,"prev_h')  # kill -9 式半行
        reloaded_ledger = EventLedger(self.ledger_path)  # 构造时截断半行
        self.addCleanup(reloaded_ledger.close)
        self.assertEqual(reloaded_ledger.last_seq, 3)
        self.assertEqual(reloaded_ledger.replay(), clean_flow_state)

    def test_run_after_crash_semantically_equivalent(self) -> None:
        # 不中断直跑（对照剧本，独立文件）
        golden_ledger = EventLedger(self.ledger_dir / "golden_run.jsonl")
        self.addCleanup(golden_ledger.close)
        append_sample_events(golden_ledger, 8)
        golden_flow_state = golden_ledger.replay()

        crashed_ledger = self.make_ledger()  # 跑 5 条后 kill 模拟（本测试主文件）
        append_sample_events(crashed_ledger, 5)
        crashed_ledger.close()
        with open(self.ledger_path, "ab") as append_handle:
            append_handle.write(b'{"seq":6,"prev_hash":"deadbeef","idem":"torn"')  # 半行
        resumed_ledger = EventLedger(self.ledger_path)  # 恢复：截断半行后续跑
        self.addCleanup(resumed_ledger.close)
        append_sample_events(resumed_ledger, 3, first_event_index=5)  # 续投第 6-8 条（剧本下标 5-7）
        self.assertEqual(resumed_ledger.last_seq, 8)  # seq 无空洞
        self.assertEqual(resumed_ledger.replay(), golden_flow_state)
        self.assertTrue(resumed_ledger.verify_chain())


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


class TestEdgeActivationTerminalGuard(LedgerTestCase):
    """K3：edge_taken 不得改写终态 target（done / failed / skipped）——投影层单一真相。"""

    def test_edge_taken_does_not_revive_failed_target(self) -> None:
        ledger = self.make_ledger()
        ledger.append(
            "flow_started",
            {"entry_nodes": ["T001"], "planned_nodes": ["T001", "T002"]},
            "run-start", RUN_NAME,
        )
        ledger.append("node_failed", {"exit_code": 3}, "T001-fail-1", RUN_NAME, "T001")
        ledger.append(
            "edge_taken", {"source": "T002", "target": "T001"}, "T002-edge-1", RUN_NAME, "T002"
        )
        self.assertEqual(ledger.replay().node_status("T001"), "failed")

    def test_edge_taken_does_not_revive_done_or_skipped_target(self) -> None:
        ledger = self.make_ledger()
        ledger.append(
            "flow_started",
            {"entry_nodes": [], "planned_nodes": ["T001", "T002"]},
            "run-start", RUN_NAME,
        )
        ledger.append("node_done", {"tokens": 0}, "T001-done-1", RUN_NAME, "T001")
        ledger.append("user_directive", {"directive": "skip"}, "T002-skip-1", RUN_NAME, "T002")
        ledger.append(
            "edge_taken", {"source": "T003", "target": "T001"}, "T003-edge-1", RUN_NAME, "T003"
        )
        ledger.append(
            "edge_taken", {"source": "T003", "target": "T002"}, "T003-edge-2", RUN_NAME, "T003"
        )
        replay_state = ledger.replay()
        self.assertEqual(replay_state.node_status("T001"), "done")
        self.assertEqual(replay_state.node_status("T002"), "skipped")

    def test_edge_taken_still_activates_planned_target(self) -> None:
        """正向对照：非终态 target 仍须被 edge_taken 置为 ready（守卫不能过宽）。"""
        ledger = self.make_ledger()
        ledger.append(
            "flow_started",
            {"entry_nodes": ["T001"], "planned_nodes": ["T001", "T002"]},
            "run-start", RUN_NAME,
        )
        ledger.append(
            "edge_taken", {"source": "T001", "target": "T002"}, "T001-edge-1", RUN_NAME, "T001"
        )
        self.assertEqual(ledger.replay().node_status("T002"), "ready")


class TestIncrementalChainVerification(LedgerTestCase):
    """K5：链校验必须增量——已确认前缀不再重复读盘/重算哈希，只校验新增条目。"""

    def append_raw_record(self, record: dict) -> None:
        with open(self.ledger_path, "ab") as append_handle:
            append_handle.write((canonical_json(record) + "\n").encode("utf-8"))

    def count_entry_hash_calls(self, invoke) -> int:
        call_counter = {"count": 0}
        original_compute_entry_hash = ledger_module.compute_entry_hash

        def counting_compute_entry_hash(entry):
            call_counter["count"] += 1
            return original_compute_entry_hash(entry)

        ledger_module.compute_entry_hash = counting_compute_entry_hash
        try:
            invoke()
        finally:
            ledger_module.compute_entry_hash = original_compute_entry_hash
        return call_counter["count"]

    def test_repeated_verify_stays_true_across_incremental_appends(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 3)
        self.assertTrue(ledger.verify_chain())
        append_sample_events(ledger, 3, first_event_index=3)
        self.assertTrue(ledger.verify_chain())
        self.assertTrue(ledger.verify_chain())

    def test_incremental_verify_only_rehashes_new_entries(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 4)
        self.assertTrue(ledger.verify_chain())
        append_sample_events(ledger, 2, first_event_index=4)

        hash_call_count = self.count_entry_hash_calls(
            lambda: self.assertTrue(ledger.verify_chain())
        )

        self.assertEqual(
            hash_call_count, 2, "增量校验只应重算新增条目的哈希（前缀不得重复读盘）"
        )

    def test_incremental_verify_detects_broken_link_in_new_entries(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 3)
        self.assertTrue(ledger.verify_chain())
        tampered_record = ledger.entries()[-1].to_dict()
        tampered_record["seq"] = 4
        tampered_record["prev_hash"] = "f" * 64
        tampered_record["idem"] = "tampered-4"
        self.append_raw_record(tampered_record)
        self.assertFalse(ledger.verify_chain())

    def test_incremental_verify_detects_seq_gap_in_new_entries(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 3)
        self.assertTrue(ledger.verify_chain())
        last_entry = ledger.entries()[-1]
        gap_record = last_entry.to_dict()
        gap_record["seq"] = 5  # seq 4 缺失
        gap_record["prev_hash"] = compute_entry_hash(last_entry)
        gap_record["idem"] = "gap-5"
        self.append_raw_record(gap_record)
        self.assertFalse(ledger.verify_chain())

    def test_incremental_verify_detects_corrupt_line_in_new_entries(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 3)
        self.assertTrue(ledger.verify_chain())
        with open(self.ledger_path, "ab") as append_handle:
            append_handle.write(b"<<< not json >>>\n")
        self.assertFalse(ledger.verify_chain())

    def test_verify_falls_back_to_full_scan_when_file_is_truncated(self) -> None:
        """文件比上次校验时更短（被截断/重写）→ 必须回退到从头全量校验。"""
        ledger = self.make_ledger()
        append_sample_events(ledger, 5)
        self.assertTrue(ledger.verify_chain())
        raw_lines = self.ledger_path.read_bytes().split(b"\n")
        tampered_record = ledger.entries()[1].to_dict()  # seq=2：其后还有条目，链能发现
        tampered_record["payload"]["tokens"] = 999999
        raw_lines[1] = canonical_json(tampered_record).encode("utf-8")
        self.ledger_path.write_bytes(b"\n".join(raw_lines[:4]))  # 截断到更短
        self.assertFalse(ledger.verify_chain())

    def test_tampering_before_first_verify_is_still_detected(self) -> None:
        ledger = self.make_ledger()
        append_sample_events(ledger, 3)
        tampered_record = ledger.entries()[1].to_dict()
        tampered_record["payload"]["tokens"] = 999999
        self.rewrite_line(1, tampered_record)
        self.assertFalse(ledger.verify_chain())


if __name__ == "__main__":
    unittest.main()
