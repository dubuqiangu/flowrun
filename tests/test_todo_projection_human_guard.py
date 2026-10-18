"""域D2 · 人审勾选门槛测试：只有处于 awaiting_human 的人审节点，勾选才生效。

回归点：改前 `apply_user_marks` 对"非 done"的人审节点勾选一律落 human_resolved，
勾选一个**尚未到达** awaiting_human 的人审节点会被立即判 done（伪造人审通过）。
现在未到达即拒绝：不判 done、不落 human_resolved 伪事件，同类告警每轮每文件至多
一条（降噪，参 T020c）。正常到达后勾选仍须照常生效（防回归）。

直跑：python -m unittest discover -s tests
"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import kernel_test_support

import kernel_test_support as support  # noqa: E402  (负责把 src/ 加进 sys.path)

from flowrun.ir.schema import NODE_KIND_HUMAN  # noqa: E402
from flowrun.kernel.todo_projection import apply_user_marks, write_todo  # noqa: E402
from flowrun.state.flow_state import (  # noqa: E402
    STATUS_AWAITING_HUMAN,
    STATUS_DONE,
    STATUS_READY,
)
from flowrun.state.ledger import EventLedger  # noqa: E402


def make_two_human_flow():
    """两个人审节点 H001（入口）→ H002，用于验证未到达即勾选的拦截与告警降噪。"""
    review_entry = support.make_node("H001", kind=NODE_KIND_HUMAN, label="首个发布人审")
    review_final = support.make_node("H002", kind=NODE_KIND_HUMAN, label="最终发布人审")
    return support.make_flow_ir(
        [review_entry, review_final],
        [support.make_edge("H001", "H002")],
    )


class HumanMarkGuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_human_guard_test_")
        self.workspace = Path(self._temp_dir.name) / "workspace"
        self.workspace.mkdir(parents=True)
        self.flow_ir = make_two_human_flow()
        self.ledger = EventLedger(self.workspace / "ledger.jsonl")
        self.todo_path = self.workspace / "todo.md"

    def tearDown(self) -> None:
        self.ledger.close()
        self._temp_dir.cleanup()

    def _prime_flow_started_only(self) -> None:
        """只落 flow_started：H001 ready、H002 planned——两者都未到达 awaiting_human。"""
        self.ledger.append(
            "flow_started",
            {"entry_nodes": ["H001"], "planned_nodes": ["H002"]},
            "run-flow-started", self.flow_ir.run_name,
        )
        write_todo(self.flow_ir, self.ledger.replay(), self.todo_path)

    def _check_todo_line(self, node_id: str) -> None:
        """把指定节点行勾上（保留行尾的状态注释与提示文本）。"""
        todo_text = self.todo_path.read_text(encoding="utf-8")
        unchecked_fragment = f"- [ ] {node_id} "
        self.assertIn(unchecked_fragment, todo_text)
        self.todo_path.write_text(
            todo_text.replace(unchecked_fragment, f"- [x] {node_id} "), encoding="utf-8"
        )

    # ---------------------------------------------------------------- 未到达即勾选

    def test_check_before_reaching_review_is_rejected_without_fake_event(self) -> None:
        self._prime_flow_started_only()
        self._check_todo_line("H001")
        flow_state = self.ledger.replay()
        self.assertEqual(flow_state.node_status("H001"), STATUS_READY)

        stderr_capture = io.StringIO()
        with redirect_stderr(stderr_capture):
            applied_count = apply_user_marks(
                self.flow_ir, self.todo_path, self.ledger, flow_state
            )

        self.assertEqual(applied_count, 0, "未到达人审即勾选不得落任何事件")
        self.assertEqual(flow_state.node_status("H001"), STATUS_READY, "节点不得被判 done")
        self.assertEqual(
            support.events_of_type(self.ledger, "human_resolved", "H001"),
            [],
            "不得产生 human_resolved 伪事件",
        )
        self.assertIn("H001", stderr_capture.getvalue(), "被拒绝必须给出可读告警")

    def test_two_premature_checks_emit_single_warning(self) -> None:
        """降噪：一轮内多个未到达人审的勾选，至多一条同类告警（不逐行刷屏）。"""
        self._prime_flow_started_only()
        self._check_todo_line("H001")
        self._check_todo_line("H002")

        stderr_capture = io.StringIO()
        with redirect_stderr(stderr_capture):
            applied_count = apply_user_marks(
                self.flow_ir, self.todo_path, self.ledger, self.ledger.replay()
            )

        self.assertEqual(applied_count, 0)
        self.assertEqual(support.events_of_type(self.ledger, "human_resolved"), [])
        warning_text = stderr_capture.getvalue()
        self.assertEqual(
            warning_text.count("尚未到达人审挂起点"),
            1,
            "每轮每文件至多一条同类告警",
        )

    # ---------------------------------------------------------------- 正常路径回归

    def test_check_after_reaching_review_lands_human_resolved(self) -> None:
        """防回归：节点到达 awaiting_human 后勾选 → 落 human_resolved 且判 done。"""
        self._prime_flow_started_only()
        self.ledger.append(
            "human_requested", {}, "run-H001-request-1", self.flow_ir.run_name, "H001"
        )
        write_todo(self.flow_ir, self.ledger.replay(), self.todo_path)
        self._check_todo_line("H001")
        flow_state = self.ledger.replay()
        self.assertEqual(flow_state.node_status("H001"), STATUS_AWAITING_HUMAN)

        applied_count = apply_user_marks(
            self.flow_ir, self.todo_path, self.ledger, flow_state
        )

        self.assertEqual(applied_count, 1)
        self.assertEqual(flow_state.node_status("H001"), STATUS_DONE)
        resolution_events = support.events_of_type(self.ledger, "human_resolved", "H001")
        self.assertEqual(len(resolution_events), 1)
        self.assertEqual(resolution_events[0].payload["decision"], "approve")

    def test_rechecking_already_done_human_node_is_silent(self) -> None:
        """已 done 的人审行重跑：零落账、零告警（与 T020c 派生勾选静默同口径）。"""
        self._prime_flow_started_only()
        self.ledger.append(
            "human_requested", {}, "run-H001-request-1", self.flow_ir.run_name, "H001"
        )
        self.ledger.append(
            "human_resolved", {"decision": "approve"},
            "run-H001-human-resolved-1", self.flow_ir.run_name, "H001",
        )
        # write_todo 对已 done 的人审行会写 [x]，此处即为勾选态，无需改写
        write_todo(self.flow_ir, self.ledger.replay(), self.todo_path)

        stderr_capture = io.StringIO()
        with redirect_stderr(stderr_capture):
            applied_count = apply_user_marks(
                self.flow_ir, self.todo_path, self.ledger, self.ledger.replay()
            )
        self.assertEqual(applied_count, 0)
        self.assertEqual(stderr_capture.getvalue(), "", "已 done 的勾选行必须静默")


if __name__ == "__main__":
    unittest.main()
