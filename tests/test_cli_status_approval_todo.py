"""CLI status 摘要 / approve·deny 人审裁决 / todo 子命令测试。

直跑：python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import cli_harness_support

from cli_harness_support import CliHarnessTestCase  # noqa: E402  (负责把 src/ 加进 sys.path)


class CliStatusApprovalTodoTestCase(CliHarnessTestCase):
    # ---------------------------------------------------------------- status

    def test_status_on_missing_workspace_exits_five(self) -> None:
        """FIX-C3：不存在的 workspace 不得伪造成空账本（退 5）。"""
        exit_code, stdout_text, stderr_text = self.invoke_cli(
            ["status", "--workspace", str(self.test_root / "nope")]
        )
        self.assertEqual(exit_code, 5)
        self.assertEqual(stdout_text, "", "不得打印伪造的全 planned 空态")
        self.assertIn("工作区未初始化", stderr_text)

    def test_todo_on_missing_workspace_exits_five(self) -> None:
        """FIX-C3：todo 子命令对不存在的工作区同样退 5。"""
        flow_path = self.test_root / "linear.mmd"
        self.write_linear_flow(flow_path)
        exit_code, stdout_text, stderr_text = self.invoke_cli(
            ["todo", "--workspace", str(self.test_root / "nope"), "--flow", str(flow_path)]
        )
        self.assertEqual(exit_code, 5)
        self.assertEqual(stdout_text, "")
        self.assertIn("工作区未初始化", stderr_text)

    # ---------------------------------------------------------------- status 摘要

    def test_status_shows_waiting_point_summary(self) -> None:
        flow_path = self.test_root / "human_gate.mmd"
        self.write_human_gate_flow(flow_path)
        self.invoke_cli(["run", str(flow_path), "--workspace", str(self.workspace)])
        exit_code, stdout_text, _stderr_text = self.invoke_cli(
            ["status", "--workspace", str(self.workspace), "--flow", str(flow_path)]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("H001 [human] awaiting_human", stdout_text)
        self.assertIn("待人审", stdout_text)
        self.assertIn("token_spent", stdout_text)
        self.assertIn("hop_count", stdout_text)

    # ---------------------------------------------------------------- approve / deny

    def test_approve_resumes_flow_to_done(self) -> None:
        flow_path = self.test_root / "human_gate.mmd"
        self.write_human_gate_flow(flow_path)
        self.invoke_cli(["run", str(flow_path), "--workspace", str(self.workspace)])
        approve_code, _approve_out, _approve_err = self.invoke_cli(
            ["approve", "H001", "--flow", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(approve_code, 0)
        resume_code, resume_stdout, _resume_err = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(resume_code, 0)
        self.assertIn("flow: done", resume_stdout)

    def test_approve_is_idempotent(self) -> None:
        flow_path = self.test_root / "human_gate.mmd"
        self.write_human_gate_flow(flow_path)
        self.invoke_cli(["run", str(flow_path), "--workspace", str(self.workspace)])
        self.invoke_cli(
            ["approve", "H001", "--flow", str(flow_path), "--workspace", str(self.workspace)]
        )
        second_code, second_stdout, _second_err = self.invoke_cli(
            ["approve", "H001", "--flow", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(second_code, 0)
        self.assertIn("跳过", second_stdout)
        ledger_lines = (self.workspace / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        human_resolution_count = sum(
            1 for line in ledger_lines if '"human_resolved"' in line
        )
        self.assertEqual(human_resolution_count, 1)

    def test_approve_before_review_point_is_refused(self) -> None:
        flow_path = self.test_root / "human_gate.mmd"
        self.write_human_gate_flow(flow_path)
        exit_code, _stdout_text, stderr_text = self.invoke_cli(
            ["approve", "H001", "--flow", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(exit_code, 1)
        self.assertIn("拒绝", stderr_text)

    def test_approve_unknown_node_reports_error(self) -> None:
        flow_path = self.test_root / "human_gate.mmd"
        self.write_human_gate_flow(flow_path)
        exit_code, _stdout_text, stderr_text = self.invoke_cli(
            ["approve", "NOPE", "--flow", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(exit_code, 1)
        self.assertIn("不存在", stderr_text)

    def test_deny_marks_review_node_failed(self) -> None:
        flow_path = self.test_root / "human_gate.mmd"
        self.write_human_gate_flow(flow_path)
        self.invoke_cli(["run", str(flow_path), "--workspace", str(self.workspace)])
        deny_code, _deny_out, _deny_err = self.invoke_cli(
            ["deny", "H001", "--flow", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(deny_code, 0)
        _status_code, status_stdout, _status_err = self.invoke_cli(
            ["status", "--workspace", str(self.workspace), "--flow", str(flow_path)]
        )
        self.assertIn("H001 [human] failed", status_stdout)

    # ---------------------------------------------------------------- todo

    def test_todo_command_applies_checkbox_and_run_resumes(self) -> None:
        flow_path = self.test_root / "human_gate.mmd"
        self.write_human_gate_flow(flow_path)
        self.invoke_cli(["run", str(flow_path), "--workspace", str(self.workspace)])
        todo_path = self.workspace / "todo.md"
        self.assertIn("- [ ] H001", todo_path.read_text(encoding="utf-8"))
        todo_path.write_text(
            todo_path.read_text(encoding="utf-8").replace("- [ ] H001", "- [x] H001"),
            encoding="utf-8",
        )
        todo_code, todo_stdout, _todo_err = self.invoke_cli(
            ["todo", "--workspace", str(self.workspace), "--flow", str(flow_path)]
        )
        self.assertEqual(todo_code, 0)
        self.assertIn("1 条", todo_stdout)
        resume_code, _resume_out, _resume_err = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(resume_code, 0)


if __name__ == "__main__":
    unittest.main()
