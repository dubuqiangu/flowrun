"""K5 · 增量链校验测试：已确认前缀不再重复读盘/重算哈希，只校验新增条目。

直跑：python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import ledger_test_support

from ledger_test_support import (  # noqa: E402  (负责把 src/ 加进 sys.path)
    LedgerTestCase,
    append_sample_events,
)
from flowrun.state import ledger as ledger_module  # noqa: E402
from flowrun.state.ledger import canonical_json, compute_entry_hash  # noqa: E402


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
