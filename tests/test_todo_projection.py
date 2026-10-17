"""域D2 · todo 投影测试（tasks.md T011）：生成格式 / 勾选落账 / @pause / @skip /
幂等 / 增删行告警。

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

from flowrun.ir.schema import NODE_KIND_DECISION, NODE_KIND_HUMAN, NODE_KIND_JOIN  # noqa: E402
from flowrun.kernel.todo_projection import (  # noqa: E402
    HUMAN_HINT,
    RESUME_HINT_TEXT,
    TODO_HEADER_COMMENT,
    TODO_MARKER_COMMENT,
    apply_user_marks,
    write_todo,
)
from flowrun.state.flow_state import STATUS_UNKNOWN  # noqa: E402
from flowrun.state.ledger import EventLedger  # noqa: E402


def make_todo_test_flow():
    """todo 测试用图：任务 → 判定 → 人审 → join，覆盖全部节点形态。"""
    task_node = support.make_node("T001", label="实现登录API", outputs=["src/auth.py"])
    second_task = support.make_node("T002", label="实现登录UI")
    decision_node = support.make_node("D001", kind=NODE_KIND_DECISION, label="验收通过?")
    human_node = support.make_node("H001", kind=NODE_KIND_HUMAN, label="发布人审")
    join_node = support.make_node("J001", kind=NODE_KIND_JOIN, label="收口")
    edges = [
        support.make_edge("T001", "T002"),
        support.make_edge("T002", "D001"),
        support.make_edge("D001", "H001", when="ok"),
        support.make_edge("H001", "J001", when="all"),
    ]
    return support.make_flow_ir(
        [task_node, second_task, decision_node, human_node, join_node], edges
    )


def make_marker_label_flow():
    """label 文本里含旧式裸标记的图：label 是流程作者的自由文本，绝不触发指令。"""
    first_task = support.make_node("T001", label="check @pause gate")
    second_task = support.make_node("T002", label="skip @skip stage")
    third_task = support.make_node("T003", label="resume @resume now")
    edges = [support.make_edge("T001", "T002"), support.make_edge("T002", "T003")]
    return support.make_flow_ir([first_task, second_task, third_task], edges)


class TodoProjectionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_todo_test_")
        self.workspace = Path(self._temp_dir.name) / "workspace"
        self.workspace.mkdir(parents=True)
        self.flow_ir = make_todo_test_flow()
        self.ledger = EventLedger(self.workspace / "ledger.jsonl")
        self.todo_path = self.workspace / "todo.md"

    def tearDown(self) -> None:
        self.ledger.close()
        self._temp_dir.cleanup()

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

    def _prime_ledger_and_write_todo(self) -> None:
        self.ledger.append(
            "flow_started",
            {"entry_nodes": ["H001"], "planned_nodes": ["T001", "T002"]},
            "run-flow-started", self.flow_ir.run_name,
        )
        self.ledger.append("node_done", {}, "run-T002-done-1", self.flow_ir.run_name, "T002")
        write_todo(self.flow_ir, self.ledger.replay(), self.todo_path)

    def _rewrite_todo_line(self, old_fragment: str, new_fragment: str) -> None:
        todo_text = self.todo_path.read_text(encoding="utf-8")
        self.assertIn(old_fragment, todo_text)
        self.todo_path.write_text(
            todo_text.replace(old_fragment, new_fragment), encoding="utf-8"
        )

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
