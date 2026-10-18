"""ledger 测试共享支撑：路径引导、可重放事件剧本、账本测试基类。

被 test_ledger*.py 复用（文件名不带 test_ 前缀，避免被 unittest 收集）。
"""

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from flowrun.state.ledger import EventLedger, canonical_json  # noqa: E402

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
