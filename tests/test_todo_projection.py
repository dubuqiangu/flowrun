"""域D2 · todo 投影测试（tasks.md T011）：生成格式 / 勾选落账 / @pause / @skip /
幂等 / 增删行告警。

直跑：python -m unittest discover -s tests
"""

import io
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import kernel_test_support

import kernel_test_support as support  # noqa: E402  (负责把 src/ 加进 sys.path)

from todo_projection_support import TodoProjectionHarnessTestCase  # noqa: E402

from flowrun.kernel.todo_projection import (  # noqa: E402
    HUMAN_HINT,
    TODO_HEADER_COMMENT,
    TODO_MARKER_COMMENT,
    apply_user_marks,
    write_todo,
)
from flowrun.state.flow_state import STATUS_UNKNOWN  # noqa: E402


class TodoProjectionTestCase(TodoProjectionHarnessTestCase):
    # ---------------------------------------------------------------- 生成格式

    def test_written_todo_has_header_trailer_and_all_node_lines(self) -> None:
        write_todo(self.flow_ir, self.ledger.replay(), self.todo_path)
        todo_text = self.todo_path.read_text(encoding="utf-8")
        todo_lines = todo_text.splitlines()
        self.assertEqual(todo_lines[0], TODO_HEADER_COMMENT)
        self.assertEqual(todo_lines[-1], TODO_MARKER_COMMENT)
        self.assertIn("- [ ] T001 实现登录API", todo_text)
        self.assertIn(f"- [ ] H001 发布人审  {HUMAN_HINT}", todo_text)
        self.assertEqual(
            len(todo_lines), 2 + len(self.flow_ir.nodes), "每个节点恰好一行"
        )

    def test_done_node_renders_checked_with_status_comment(self) -> None:
        self.ledger.append(
            "flow_started",
            {"entry_nodes": ["T001"], "planned_nodes": ["T002", "D001", "H001", "J001"]},
            "run-flow-started", self.flow_ir.run_name,
        )
        self.ledger.append("node_done", {"artifacts": ["src/auth.py"]},
                           "run-T001-done-1", self.flow_ir.run_name, "T001")
        write_todo(self.flow_ir, self.ledger.replay(), self.todo_path)
        todo_text = self.todo_path.read_text(encoding="utf-8")
        self.assertIn("- [x] T001 实现登录API  <!-- done -->", todo_text)
        self.assertIn("- [ ] T002 实现登录UI", todo_text)

    def test_failed_and_awaiting_human_render_status_comments(self) -> None:
        self.ledger.append("flow_started", {"planned_nodes": ["T001", "H001"]},
                           "run-flow-started", self.flow_ir.run_name)
        self.ledger.append("node_failed", {"exit_code": 3},
                           "run-T001-failed-1", self.flow_ir.run_name, "T001")
        self.ledger.append("human_requested", {},
                           "run-H001-request-1", self.flow_ir.run_name, "H001")
        write_todo(self.flow_ir, self.ledger.replay(), self.todo_path)
        todo_text = self.todo_path.read_text(encoding="utf-8")
        self.assertIn("- [ ] T001 实现登录API  <!-- failed -->", todo_text)
        self.assertIn(f"- [ ] H001 发布人审  {HUMAN_HINT}  <!-- awaiting_human -->", todo_text)

    # ---------------------------------------------------------------- 用户三操作落账

    def test_human_check_appends_human_resolved_and_updates_state(self) -> None:
        self._prime_ledger_and_write_todo()
        self._rewrite_todo_line("- [ ] H001 发布人审", "- [x] H001 发布人审")
        flow_state = self.ledger.replay()
        applied_count = apply_user_marks(self.flow_ir, self.todo_path, self.ledger, flow_state)
        self.assertEqual(applied_count, 1)
        self.assertEqual(flow_state.node_status("H001"), "done")
        resolution_events = support.events_of_type(self.ledger, "human_resolved", "H001")
        self.assertEqual(len(resolution_events), 1)
        self.assertEqual(resolution_events[0].payload["decision"], "approve")

    def test_pause_directive_appends_user_directive(self) -> None:
        self._prime_ledger_and_write_todo()
        self._rewrite_todo_line(
            "- [ ] T001 实现登录API", "- [ ] T001 实现登录API <!-- @pause -->"
        )
        flow_state = self.ledger.replay()
        applied_count = apply_user_marks(self.flow_ir, self.todo_path, self.ledger, flow_state)
        self.assertEqual(applied_count, 1)
        self.assertTrue(flow_state.is_paused())
        self.assertEqual(flow_state.node_status("T001"), "paused")
        directive_events = support.events_of_type(self.ledger, "user_directive", "T001")
        self.assertEqual(directive_events[0].payload["directive"], "pause")

    def test_skip_directive_marks_node_skipped(self) -> None:
        self._prime_ledger_and_write_todo()
        self._rewrite_todo_line(
            "- [ ] T001 实现登录API", "- [ ] T001 实现登录API <!-- @skip -->"
        )
        flow_state = self.ledger.replay()
        applied_count = apply_user_marks(self.flow_ir, self.todo_path, self.ledger, flow_state)
        self.assertEqual(applied_count, 1)
        self.assertEqual(flow_state.node_status("T001"), "skipped")
        directive_events = support.events_of_type(self.ledger, "user_directive", "T001")
        self.assertEqual(directive_events[0].payload["directive"], "skip")

    def test_reapplying_same_marks_is_idempotent(self) -> None:
        self._prime_ledger_and_write_todo()
        self._rewrite_todo_line("- [ ] H001 发布人审", "- [x] H001 发布人审")
        self._rewrite_todo_line(
            "- [ ] T001 实现登录API", "- [ ] T001 实现登录API <!-- @skip -->"
        )
        flow_state = self.ledger.replay()
        self.assertEqual(
            apply_user_marks(self.flow_ir, self.todo_path, self.ledger, flow_state), 2
        )
        event_count_after_first = len(self.ledger.entries())
        flow_state_after_first = self.ledger.replay()
        second_count = apply_user_marks(
            self.flow_ir, self.todo_path, self.ledger, flow_state_after_first
        )
        self.assertEqual(second_count, 0)
        self.assertEqual(len(self.ledger.entries()), event_count_after_first)

    # ---------------------------------------------------------------- 暂停可见与 @resume（FIX-C1/C4）

    def test_paused_flow_renders_banner_and_resume_marker_clears_it(self) -> None:
        """@pause 后 todo 头部有 FLOW PAUSED；@resume 落账后横幅消失（FIX-C4）。"""
        self._prime_ledger_and_write_todo()
        self._rewrite_todo_line(
            "- [ ] T001 实现登录API", "- [ ] T001 实现登录API <!-- @pause -->"
        )
        flow_state = self.ledger.replay()
        self.assertEqual(
            apply_user_marks(self.flow_ir, self.todo_path, self.ledger, flow_state), 1
        )
        write_todo(self.flow_ir, flow_state, self.todo_path, self.ledger)
        paused_todo_text = self.todo_path.read_text(encoding="utf-8")
        self.assertIn("FLOW PAUSED", paused_todo_text)
        self.assertIn("flowrun resume", paused_todo_text)
        self.assertIn("<!-- paused -->", paused_todo_text, "节点级暂停态必须可见")

        self._rewrite_todo_line(
            "- [ ] T001 实现登录API", "- [ ] T001 实现登录API <!-- @resume -->"
        )
        self.assertEqual(
            apply_user_marks(self.flow_ir, self.todo_path, self.ledger, flow_state), 1
        )
        self.assertFalse(flow_state.is_paused())
        write_todo(self.flow_ir, flow_state, self.todo_path, self.ledger)
        self.assertNotIn(
            "FLOW PAUSED",
            self.todo_path.read_text(encoding="utf-8"),
            "恢复后暂停横幅必须消失",
        )

    def test_resume_marker_lands_flow_resumed_and_is_idempotent(self) -> None:
        """todo @resume → flow_resumed(by=todo)；已非暂停再跑只告警不落账（FIX-C1/C5）。"""
        self._prime_ledger_and_write_todo()
        self._rewrite_todo_line(
            "- [ ] T001 实现登录API", "- [ ] T001 实现登录API <!-- @pause -->"
        )
        pausing_state = self.ledger.replay()
        apply_user_marks(self.flow_ir, self.todo_path, self.ledger, pausing_state)
        # 工具应用标记后会重写 todo（抹掉 @pause），用户再在派生文件上加 @resume
        write_todo(self.flow_ir, pausing_state, self.todo_path, self.ledger)
        self._rewrite_todo_line(
            "- [ ] T001 实现登录API", "- [ ] T001 实现登录API <!-- @resume -->"
        )
        self.assertEqual(
            apply_user_marks(self.flow_ir, self.todo_path, self.ledger, pausing_state), 1
        )
        resume_events = support.events_of_type(self.ledger, "flow_resumed")
        self.assertEqual(len(resume_events), 1)
        self.assertEqual(resume_events[0].payload["by"], "todo")

        stderr_capture = io.StringIO()
        with redirect_stderr(stderr_capture):
            second_count = apply_user_marks(
                self.flow_ir, self.todo_path, self.ledger, self.ledger.replay()
            )
        self.assertEqual(second_count, 0)
        self.assertEqual(len(support.events_of_type(self.ledger, "flow_resumed")), 1)
        self.assertIn("@resume", stderr_capture.getvalue())

    # ---------------------------------------------------------------- 增删行与非法勾选

    def test_unknown_and_missing_node_lines_only_warn(self) -> None:
        self._prime_ledger_and_write_todo()
        todo_text = self.todo_path.read_text(encoding="utf-8")
        todo_text = todo_text.replace(
            "- [ ] T001 实现登录API\n", "- [ ] T999 幽灵节点\n"
        )
        self.todo_path.write_text(todo_text, encoding="utf-8")
        stderr_capture = io.StringIO()
        with redirect_stderr(stderr_capture):
            applied_count = apply_user_marks(
                self.flow_ir, self.todo_path, self.ledger, self.ledger.replay()
            )
        self.assertEqual(applied_count, 0)
        warning_text = stderr_capture.getvalue()
        self.assertIn("T999", warning_text)
        self.assertIn("T001", warning_text)

    def test_checking_non_human_node_is_ignored_with_warning(self) -> None:
        self._prime_ledger_and_write_todo()
        self._rewrite_todo_line("- [ ] T001 实现登录API", "- [x] T001 实现登录API")
        stderr_capture = io.StringIO()
        with redirect_stderr(stderr_capture):
            applied_count = apply_user_marks(
                self.flow_ir, self.todo_path, self.ledger, self.ledger.replay()
            )
        self.assertEqual(applied_count, 0)
        self.assertEqual(support.events_of_type(self.ledger, "human_resolved"), [])
        self.assertIn("人审", stderr_capture.getvalue())

    def test_missing_todo_file_returns_zero(self) -> None:
        flow_state = self.ledger.replay()
        self.assertEqual(
            apply_user_marks(self.flow_ir, self.todo_path, self.ledger, flow_state), 0
        )
        self.assertEqual(flow_state.node_status("T001"), STATUS_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
