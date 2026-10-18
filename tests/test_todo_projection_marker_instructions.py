"""域D2 · todo 指令形式与幂等测试（L3）：指令只认注释形式、旧式裸标记只告警不落账、
派生文件自解析零告警。

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

from todo_projection_support import (  # noqa: E402
    TodoProjectionHarnessTestCase,
    make_marker_label_flow,
)

from flowrun.kernel.todo_projection import (  # noqa: E402
    RESUME_HINT_TEXT,
    TODO_HEADER_COMMENT,
    TODO_MARKER_COMMENT,
    apply_user_marks,
    write_todo,
)


class TodoMarkerInstructionTestCase(TodoProjectionHarnessTestCase):
    def test_marker_comment_documents_resume(self) -> None:
        """头部使用说明文案必须写进 @resume。"""
        self.assertIn("@resume", TODO_MARKER_COMMENT)

    def test_marker_texts_avoid_nested_html_comments(self) -> None:
        """头尾文案与横幅提示都嵌在 HTML 注释里，不得再含内层 "<!--"（会提前闭合）。"""
        self.assertEqual(TODO_HEADER_COMMENT.count("<!--"), 1)
        self.assertEqual(TODO_MARKER_COMMENT.count("<!--"), 1)
        self.assertEqual(TODO_MARKER_COMMENT.count("-->"), 1)
        self.assertNotIn("<!--", RESUME_HINT_TEXT)

    def test_derived_done_checks_are_silent_on_reapply(self) -> None:
        """FIX-C5：已 done 的派生 [x] 行重跑不得重复告警（连跑两次均零告警）。"""
        self.ledger.append(
            "flow_started",
            {"entry_nodes": ["T001"], "planned_nodes": ["T002", "D001", "H001", "J001"]},
            "run-flow-started", self.flow_ir.run_name,
        )
        self.ledger.append(
            "node_done", {}, "run-T001-done-1", self.flow_ir.run_name, "T001"
        )
        write_todo(self.flow_ir, self.ledger.replay(), self.todo_path)
        self.assertIn(
            "- [x] T001 实现登录API", self.todo_path.read_text(encoding="utf-8")
        )

        stderr_capture = io.StringIO()
        with redirect_stderr(stderr_capture):
            self.assertEqual(
                apply_user_marks(
                    self.flow_ir, self.todo_path, self.ledger, self.ledger.replay()
                ),
                0,
            )
        self.assertEqual(stderr_capture.getvalue(), "", "派生勾选行不得告警")

        second_stderr_capture = io.StringIO()
        with redirect_stderr(second_stderr_capture):
            self.assertEqual(
                apply_user_marks(
                    self.flow_ir, self.todo_path, self.ledger, self.ledger.replay()
                ),
                0,
            )
        self.assertEqual(second_stderr_capture.getvalue(), "", "第二次重跑零告警")

    # ---------------------------------------------------------------- 指令只认注释形式（L3）

    def _write_marker_label_todo(self):
        """label 含裸标记的图写一份 todo（label 文本必须只当文本看）。"""
        marker_flow_ir = make_marker_label_flow()
        self.ledger.append(
            "flow_started",
            {"entry_nodes": ["T001"], "planned_nodes": ["T001", "T002", "T003"]},
            "run-flow-started",
            marker_flow_ir.run_name,
        )
        write_todo(marker_flow_ir, self.ledger.replay(), self.todo_path)
        return marker_flow_ir

    def test_label_text_with_bare_markers_never_lands_events(self) -> None:
        """回归点：label 里的 @pause/@skip/@resume 是文本，不落任何事件、不暂停流。"""
        marker_flow_ir = self._write_marker_label_todo()
        todo_text = self.todo_path.read_text(encoding="utf-8")
        self.assertIn("check @pause gate", todo_text)
        self.assertIn("skip @skip stage", todo_text)
        self.assertIn("resume @resume now", todo_text)

        flow_state = self.ledger.replay()
        stderr_capture = io.StringIO()
        with redirect_stderr(stderr_capture):
            applied_count = apply_user_marks(
                marker_flow_ir, self.todo_path, self.ledger, flow_state
            )
        self.assertEqual(applied_count, 0)
        self.assertFalse(flow_state.is_paused())
        self.assertEqual(support.events_of_type(self.ledger, "user_directive"), [])
        self.assertEqual(support.events_of_type(self.ledger, "flow_resumed"), [])
        self.assertEqual(
            stderr_capture.getvalue().count("旧式裸标记"),
            1,
            "旧式裸标记每轮每文件至多一条升级提示（防刷屏）",
        )

    def test_bare_marker_label_does_not_repause_done_flow(self) -> None:
        """回归点（最严重）：done 的流不得因 label 文本里的 @pause 被重新置为 paused。"""
        marker_flow_ir = self._write_marker_label_todo()
        self.ledger.append("flow_done", {}, "run-flow-done", marker_flow_ir.run_name)
        flow_state = self.ledger.replay()
        self.assertTrue(flow_state.is_done())

        with redirect_stderr(io.StringIO()):
            applied_count = apply_user_marks(
                marker_flow_ir, self.todo_path, self.ledger, flow_state
            )
        self.assertEqual(applied_count, 0)
        self.assertTrue(flow_state.is_done())
        self.assertFalse(flow_state.is_paused(), "done 的流不得被 label 文本重新暂停")

    def test_comment_form_markers_land_all_three_directives(self) -> None:
        """注释形式：<!-- @pause --> / <!-- @skip --> 落 user_directive，<!-- @resume --> 落 flow_resumed。"""
        marker_flow_ir = self._write_marker_label_todo()
        self._rewrite_todo_line(
            "- [ ] T001 check @pause gate",
            "- [ ] T001 check @pause gate <!-- @pause -->",
        )
        self._rewrite_todo_line(
            "- [ ] T002 skip @skip stage",
            "- [ ] T002 skip @skip stage <!-- @skip -->",
        )
        self._rewrite_todo_line(
            "- [ ] T003 resume @resume now",
            "- [ ] T003 resume @resume now <!-- @resume -->",
        )
        flow_state = self.ledger.replay()
        applied_count = apply_user_marks(
            marker_flow_ir, self.todo_path, self.ledger, flow_state
        )
        self.assertEqual(applied_count, 3, "pause + skip + resume 各落一条")
        self.assertEqual(
            flow_state.node_status("T002"), "skipped", "注释形式的 @skip 生效"
        )
        self.assertEqual(
            flow_state.node_status("T001"),
            "ready",
            "@resume 把 T001 恢复到暂停前状态（ready）——既有恢复语义不变",
        )
        self.assertFalse(flow_state.is_paused(), "注释形式的 @resume 应解除全局暂停")
        landed_directives = {
            entry.payload["directive"]
            for entry in support.events_of_type(self.ledger, "user_directive")
        }
        self.assertEqual(landed_directives, {"pause", "skip"})
        resume_events = support.events_of_type(self.ledger, "flow_resumed")
        self.assertEqual(len(resume_events), 1)
        self.assertEqual(resume_events[0].payload["by"], "todo")

    def test_generated_todo_is_idempotent_under_apply_user_marks(self) -> None:
        """工具自己写出的 todo 再被自己解析：零落账、零告警（语法不会自伤）。"""
        self._prime_ledger_and_write_todo()
        write_todo(self.flow_ir, self.ledger.replay(), self.todo_path, self.ledger)
        event_count_before = len(self.ledger.entries())

        stderr_capture = io.StringIO()
        with redirect_stderr(stderr_capture):
            applied_count = apply_user_marks(
                self.flow_ir, self.todo_path, self.ledger, self.ledger.replay()
            )
        self.assertEqual(applied_count, 0)
        self.assertEqual(len(self.ledger.entries()), event_count_before)
        self.assertEqual(stderr_capture.getvalue(), "", "生成文件不得触发裸标记告警")

    def test_legacy_bare_marker_warns_once_and_lands_nothing(self) -> None:
        """升级兼容：用户仍写裸标记 → 一条可读告警 + 零落账（不静默失效）。"""
        self._prime_ledger_and_write_todo()
        self._rewrite_todo_line(
            "- [ ] T001 实现登录API", "- [ ] T001 实现登录API @pause"
        )
        stderr_capture = io.StringIO()
        with redirect_stderr(stderr_capture):
            applied_count = apply_user_marks(
                self.flow_ir, self.todo_path, self.ledger, self.ledger.replay()
            )
        self.assertEqual(applied_count, 0)
        self.assertEqual(
            support.events_of_type(self.ledger, "user_directive"),
            [],
            "旧式裸标记不得再落账",
        )
        warning_text = stderr_capture.getvalue()
        self.assertIn("旧式裸标记", warning_text)
        self.assertEqual(warning_text.count("旧式裸标记"), 1, "每轮每文件至多一条告警")


if __name__ == "__main__":
    unittest.main()
