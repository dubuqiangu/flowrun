"""域B · ledger 测试（tasks.md T004）：追加语义、哈希链校验、幂等重放、崩溃恢复。

直跑：python -m unittest discover -s tests
"""

import json
import sys
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import ledger_test_support

from ledger_test_support import (  # noqa: E402  (负责把 src/ 加进 sys.path)
    RUN_NAME,
    SAMPLE_EVENTS,
    LedgerTestCase,
    append_sample_events,
)
from flowrun.state.ledger import (  # noqa: E402
    GENESIS_PREV_HASH,
    EventLedger,
    canonical_json,
    compute_entry_hash,
)


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


if __name__ == "__main__":
    unittest.main()
