"""域B · 快照与恢复测试（tasks.md T005）：周期快照、原子写、快照+增量重放等价、防篡改。

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

from flowrun.state.flow_state import FlowState  # noqa: E402
from flowrun.state.ledger import (  # noqa: E402
    LedgerCorruptionError,
    EventLedger,
    canonical_json,
    compute_entry_hash,
)
from flowrun.state.snapshots import SnapshotStore, recover_flow_state  # noqa: E402

RUN_NAME = "release-login"

# 与 test_ledger.py 相同结构的自洽剧本（测试文件间不互相 import，保持各域测试可独立直跑）
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


class SnapshotTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_snapshot_test_")
        self.addCleanup(temp_dir.cleanup)
        self.work_dir = Path(temp_dir.name)
        self.ledger_path = self.work_dir / "ledger.jsonl"
        self.snapshot_dir = self.work_dir / "snapshots"

    def make_ledger(self) -> EventLedger:
        ledger = EventLedger(self.ledger_path)
        self.addCleanup(ledger.close)
        return ledger

    def make_store(self, every_n_events: int = 5) -> SnapshotStore:
        return SnapshotStore(self.snapshot_dir, every_n_events=every_n_events)

    def run_events_with_snapshots(
        self,
        ledger: EventLedger,
        snapshot_store: SnapshotStore,
        flow_state: FlowState,
        event_count: int,
        first_event_index: int = 0,
    ) -> FlowState:
        """内核式推进：追加 → 状态投影 → 试落快照（快照点由账本长度决定）。"""
        for event_type, payload, idem_key, node_id in SAMPLE_EVENTS[
            first_event_index : first_event_index + event_count
        ]:
            entry = ledger.append(event_type, payload, idem_key, RUN_NAME, node_id)
            flow_state.apply_event(entry)
            snapshot_store.maybe_snapshot(ledger, flow_state)
        return flow_state

    def tamper_ledger_line(self, line_index: int, mutated_record: dict) -> None:
        raw_lines = self.ledger_path.read_bytes().split(b"\n")
        raw_lines[line_index] = canonical_json(mutated_record).encode("utf-8")
        self.ledger_path.write_bytes(b"\n".join(raw_lines))


class TestSnapshotStore(SnapshotTestCase):
    def test_maybe_snapshot_writes_only_on_interval(self) -> None:
        ledger = self.make_ledger()
        snapshot_store = self.make_store(every_n_events=5)
        flow_state = FlowState()
        returned_paths = []
        for event_type, payload, idem_key, node_id in SAMPLE_EVENTS[:7]:
            entry = ledger.append(event_type, payload, idem_key, RUN_NAME, node_id)
            flow_state.apply_event(entry)
            returned_paths.append(snapshot_store.maybe_snapshot(ledger, flow_state))
        self.assertTrue(all(path is None for path in returned_paths[:4]))
        self.assertIsNotNone(returned_paths[4])  # seq=5 命中周期
        self.assertTrue(all(path is None for path in returned_paths[5:]))  # 6、7 不命中
        self.assertEqual(
            sorted(path.name for path in self.snapshot_dir.iterdir()),
            [f"snapshot_{5:010d}.json"],
        )

    def test_maybe_snapshot_skips_duplicate_at_same_seq(self) -> None:
        ledger = self.make_ledger()
        snapshot_store = self.make_store(every_n_events=5)
        flow_state = FlowState()
        self.run_events_with_snapshots(ledger, snapshot_store, flow_state, 5)
        self.assertIsNone(snapshot_store.maybe_snapshot(ledger, flow_state))  # 同 seq 不重落

    def test_load_latest_returns_state_and_seq(self) -> None:
        ledger = self.make_ledger()
        snapshot_store = self.make_store(every_n_events=5)
        flow_state = self.run_events_with_snapshots(ledger, snapshot_store, FlowState(), 7)
        loaded = snapshot_store.load_latest()
        self.assertIsNotNone(loaded)
        loaded_state, loaded_seq = loaded
        self.assertEqual(loaded_seq, 5)
        expected_snapshot_state = FlowState().merge_from_events(ledger.entries()[:5])
        self.assertEqual(loaded_state, expected_snapshot_state)  # 状态恰为快照点投影
        self.assertNotEqual(loaded_state, ledger.replay())  # 当前 head(7 条) 已越过快照点

    def test_load_latest_none_when_no_snapshots(self) -> None:
        self.assertIsNone(self.make_store().load_latest())

    def test_snapshot_file_is_valid_record(self) -> None:
        ledger = self.make_ledger()
        snapshot_store = self.make_store(every_n_events=5)
        self.run_events_with_snapshots(ledger, snapshot_store, FlowState(), 5)
        snapshot_files = list(self.snapshot_dir.iterdir())
        self.assertEqual(len(snapshot_files), 1)
        self.assertFalse(any(path.name.startswith(".") for path in snapshot_files))  # 无 temp 残留
        snapshot_record = json.loads(snapshot_files[0].read_bytes().decode("utf-8"))
        self.assertEqual(snapshot_record["seq"], 5)
        self.assertEqual(snapshot_record["head_hash"], compute_entry_hash(ledger.entries()[4]))
        self.assertEqual(FlowState.from_dict(snapshot_record["state"]), ledger.replay())

    def test_every_n_events_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            SnapshotStore(self.snapshot_dir, every_n_events=0)


class TestRecovery(SnapshotTestCase):
    def test_recover_without_snapshots_equals_full_replay(self) -> None:
        ledger = self.make_ledger()
        snapshot_store = self.make_store(every_n_events=5)
        self.run_events_with_snapshots(ledger, snapshot_store, FlowState(), 3)  # 从未命中周期
        self.assertEqual(recover_flow_state(ledger, snapshot_store), ledger.replay())

    def test_snapshot_plus_incremental_equals_full_replay(self) -> None:
        ledger = self.make_ledger()
        snapshot_store = self.make_store(every_n_events=5)
        self.run_events_with_snapshots(ledger, snapshot_store, FlowState(), 16)
        # 跨快照边界的重复投递：idem 与 seq=2 的 node_entered 相同但内容不同（seq 17-18）
        ledger.append("node_done", {"tokens": 999999}, "T001-enter-1", RUN_NAME, "T001")
        ledger.append("agent_dispatched", {}, "T002-dispatch-1", RUN_NAME, "T002")
        recovered_state = recover_flow_state(ledger, snapshot_store)
        self.assertEqual(recovered_state, ledger.replay())  # 快照恢复 == 全量重放
        self.assertEqual(recovered_state.token_spent(), 2550)  # 重复条目的 tokens 未被计入

    def test_crash_resume_final_state_equals_uninterrupted_run(self) -> None:
        # 不中断直跑（对照组）
        golden_ledger = EventLedger(self.work_dir / "golden_run.jsonl")
        self.addCleanup(golden_ledger.close)
        golden_store = SnapshotStore(self.work_dir / "golden_snapshots", every_n_events=5)
        golden_final_state = self.run_events_with_snapshots(
            golden_ledger, golden_store, FlowState(), 12
        )

        # 中断剧本：跑 5 条落快照 → kill 模拟半行 → 重启恢复 → 续跑到第 12 条
        interrupted_ledger = self.make_ledger()
        interrupted_store = self.make_store(every_n_events=5)
        self.run_events_with_snapshots(interrupted_ledger, interrupted_store, FlowState(), 5)
        interrupted_ledger.close()
        with open(self.ledger_path, "ab") as append_handle:
            append_handle.write(b'{"seq":6,"prev_hash":"deadbeef","idem":"torn"')  # 半行
        resumed_ledger = EventLedger(self.ledger_path)  # 重启：截断半行
        self.addCleanup(resumed_ledger.close)
        resumed_state = recover_flow_state(resumed_ledger, interrupted_store)  # 快照点恢复
        self.assertEqual(resumed_state, resumed_ledger.replay())  # 恢复 == 当刻全量重放
        self.run_events_with_snapshots(resumed_ledger, interrupted_store, resumed_state, 7,
                                       first_event_index=5)
        self.assertEqual(resumed_state, golden_final_state)  # 终态与不中断直跑等价
        self.assertEqual(resumed_ledger.replay(), golden_ledger.replay())  # 全量重放亦一致

    def test_recover_detects_tampering_within_snapshot_coverage(self) -> None:
        ledger = self.make_ledger()
        snapshot_store = self.make_store(every_n_events=5)
        self.run_events_with_snapshots(ledger, snapshot_store, FlowState(), 5)
        ledger.close()
        tampered_record = ledger.entries()[1].to_dict()  # seq=2，在快照覆盖范围内
        tampered_record["payload"]["tokens"] = 999999
        self.tamper_ledger_line(1, tampered_record)
        with self.assertRaises(LedgerCorruptionError):
            recover_flow_state(EventLedger(self.ledger_path), snapshot_store)


if __name__ == "__main__":
    unittest.main()
