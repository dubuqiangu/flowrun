"""K3 · edge_taken 终态守卫测试：不得改写终态 target（done / failed / skipped）。

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
)


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


if __name__ == "__main__":
    unittest.main()
