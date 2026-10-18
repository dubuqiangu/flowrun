"""只读账本视图（EventLedger.open_readonly）的行为测试。

重点：只读视图不截断崩溃半行、不持有写句柄、禁止追加；判定侧代理不再产生写副作用。
"""

import json
import tempfile
import unittest
from pathlib import Path

from flowrun.kernel.hooks_assembly import _JudgeLedgerRecorder
from flowrun.state.ledger import EventLedger


def _write_ledger_with_trailing_half_line(ledger_path: Path, complete_entry_count: int) -> bytes:
    """写 complete_entry_count 条完整事件，再手工追加一段无换行结尾的半行垃圾。"""
    ledger = EventLedger(ledger_path)
    try:
        for event_index in range(complete_entry_count):
            ledger.append(
                event_type="node_started",
                payload={"index": event_index},
                idem_key=f"event-{event_index}",
                run_name="run-readonly",
                node_id=f"T{event_index:03d}",
            )
    finally:
        ledger.close()
    with open(ledger_path, "ab") as append_handle:
        append_handle.write(b'{"seq":99,"prev_hash":"truncated-garbage')
    return ledger_path.read_bytes()


class ReadonlyViewKeepsBytesTest(unittest.TestCase):
    """只读打开绝不改写磁盘字节。"""

    def test_readonly_view_ignores_tail_without_mutating_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = Path(temp_dir) / "ledger.jsonl"
            bytes_before_open = _write_ledger_with_trailing_half_line(ledger_path, 1)

            readonly_view = EventLedger.open_readonly(ledger_path)
            entries = readonly_view.entries()

            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].idem, "event-0")
            self.assertEqual(ledger_path.read_bytes(), bytes_before_open)
            self.assertIn(b"truncated-garbage", ledger_path.read_bytes())
            self.assertIsNone(readonly_view._write_handle)
            readonly_view.close()

    def test_writer_mode_truncates_tail_for_contrast(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = Path(temp_dir) / "ledger.jsonl"
            bytes_before_open = _write_ledger_with_trailing_half_line(ledger_path, 1)

            writer_ledger = EventLedger(ledger_path)
            try:
                bytes_after_open = ledger_path.read_bytes()
                self.assertNotEqual(bytes_after_open, bytes_before_open)
                self.assertNotIn(b"truncated-garbage", bytes_after_open)
                self.assertEqual(len(writer_ledger.entries()), 1)
            finally:
                writer_ledger.close()

    def test_readonly_append_raises_and_keeps_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = Path(temp_dir) / "ledger.jsonl"
            bytes_before_open = _write_ledger_with_trailing_half_line(ledger_path, 2)

            readonly_view = EventLedger.open_readonly(ledger_path)
            with self.assertRaises(RuntimeError) as raised:
                readonly_view.append(
                    event_type="gate_tripped",
                    payload={"gate": "verdict"},
                    idem_key="readonly-append",
                    run_name="run-readonly",
                )
            self.assertIn("只读账本视图不支持追加", str(raised.exception))
            self.assertEqual(ledger_path.read_bytes(), bytes_before_open)

            # 校验放在 payload 规范化之前：非法 payload 也先报只读错误
            with self.assertRaises(RuntimeError):
                readonly_view.append(
                    event_type="gate_tripped",
                    payload={"unserializable": object()},
                    idem_key="readonly-append",
                    run_name="run-readonly",
                )
            self.assertEqual(ledger_path.read_bytes(), bytes_before_open)

    def test_readonly_view_query_apis_match_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = Path(temp_dir) / "ledger.jsonl"
            _write_ledger_with_trailing_half_line(ledger_path, 3)

            writer_ledger = EventLedger(ledger_path)
            try:
                readonly_view = EventLedger.open_readonly(ledger_path)
                self.assertTrue(readonly_view.verify_chain())
                self.assertEqual(readonly_view.last_seq, writer_ledger.last_seq)
                self.assertEqual(readonly_view.head_hash, writer_ledger.head_hash)
                replay_state = readonly_view.replay()
                self.assertIsNotNone(replay_state)
            finally:
                writer_ledger.close()


class ReadonlyViewHashChainTest(unittest.TestCase):
    """正常追加路径的哈希链在只读视图中仍一致。"""

    def test_write_then_readonly_chain_consistency(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = Path(temp_dir) / "ledger.jsonl"
            writer_ledger = EventLedger(ledger_path)
            try:
                for event_index in range(3):
                    writer_ledger.append(
                        event_type="agent_exited",
                        payload={"exit_code": 0, "index": event_index},
                        idem_key=f"chain-event-{event_index}",
                        run_name="run-chain",
                        node_id=f"T{event_index:03d}",
                    )
                self.assertTrue(writer_ledger.verify_chain())

                readonly_view = EventLedger.open_readonly(ledger_path)
                self.assertTrue(readonly_view.verify_chain())
                self.assertEqual(readonly_view.last_seq, 3)
                self.assertEqual(readonly_view.last_seq, writer_ledger.last_seq)
                self.assertEqual(readonly_view.head_hash, writer_ledger.head_hash)
                self.assertEqual(
                    [entry.idem for entry in readonly_view.entries()],
                    ["chain-event-0", "chain-event-1", "chain-event-2"],
                )
            finally:
                writer_ledger.close()


class JudgeLedgerRecorderReadonlyTest(unittest.TestCase):
    """判定侧代理读取账本时不截断、不改写磁盘。"""

    def test_judge_ledger_recorder_entries_ignore_tail_without_mutating_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = Path(temp_dir) / "ledger.jsonl"
            bytes_before_open = _write_ledger_with_trailing_half_line(ledger_path, 2)

            recorder = _JudgeLedgerRecorder(ledger_path)
            entries = recorder.entries()
            entries_again = recorder.entries()

            self.assertEqual([entry.idem for entry in entries], ["event-0", "event-1"])
            self.assertEqual(len(entries_again), 2)
            self.assertEqual(ledger_path.read_bytes(), bytes_before_open)

            # 代理的写路径只记录，不落盘
            recorder.append(
                event_type="gate_tripped",
                payload={"gate": "verdict"},
                idem_key="judge-trip",
                run_name="run-readonly",
            )
            self.assertEqual(len(recorder.recorded_events), 1)
            self.assertEqual(ledger_path.read_bytes(), bytes_before_open)

    def test_judge_ledger_recorder_sees_newly_appended_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger_path = Path(temp_dir) / "ledger.jsonl"
            writer_ledger = EventLedger(ledger_path)
            try:
                writer_ledger.append(
                    event_type="node_started",
                    payload={},
                    idem_key="first",
                    run_name="run-chain",
                )
                first_recorder = _JudgeLedgerRecorder(ledger_path)
                self.assertEqual(len(first_recorder.entries()), 1)

                writer_ledger.append(
                    event_type="node_started",
                    payload={},
                    idem_key="second",
                    run_name="run-chain",
                )
                # 每次判定都会新建 recorder，因此能看到引擎新落账的条目
                second_recorder = _JudgeLedgerRecorder(ledger_path)
                self.assertEqual(len(second_recorder.entries()), 2)
            finally:
                writer_ledger.close()


if __name__ == "__main__":
    unittest.main()