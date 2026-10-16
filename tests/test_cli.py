"""CLI 装配层测试：compile 好/坏图、run 退出码口径、resume 恢复通道、
approve/deny 幂等与拒绝、status 摘要、todo 子命令（真子进程假 agent，零配置直跑）。

退出码契约：0=done / 1=waiting / 2=halted / 3=编译失败 / 4=工作区身份不匹配 /
5=工作区未初始化。

直跑：python -m unittest discover -s tests
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import sys

TESTS_DIRECTORY = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIRECTORY.parent
SOURCE_ROOT = PROJECT_ROOT / "src"
for path_entry in (str(SOURCE_ROOT), str(TESTS_DIRECTORY)):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)

from flowrun.cli import main as cli_main  # noqa: E402

FIXTURES_DIRECTORY = TESTS_DIRECTORY / "fixtures"
PYTHON_EXECUTABLE_POSIX = Path(sys.executable).as_posix()


def fake_agent_command_text(script_name: str, mode: str | None = None) -> str:
    """假 agent 的命令模板（路径用 POSIX 斜杠，shlex 免转义；单引号包住防
    frontmatter 把内部双引号当值边界）。"""
    script_path_posix = (FIXTURES_DIRECTORY / script_name).as_posix()
    command_text = f'"{PYTHON_EXECUTABLE_POSIX}" "{script_path_posix}"'
    if mode is not None:
        command_text += f" --mode {mode}"
    return command_text


def compose_flow_document(
    run_name: str,
    agent_command_by_name: dict[str, str],
    flow_body_lines: list[str],
    default_agent: str = "fake-task",
    budget_tokens: int = 100000,
) -> str:
    """按编译器契约拼一份完整 flow.mmd 文本（frontmatter + flowchart）。"""
    agent_declaration_lines = [
        f"  {agent_name}: {{cmd: '{command_text}'}}"
        for agent_name, command_text in agent_command_by_name.items()
    ]
    document_lines = [
        "---",
        f"run: {run_name}",
        f"defaults: {{agent: {default_agent}, budget_tokens: {budget_tokens}}}",
        "agents:",
        *agent_declaration_lines,
        "---",
        "flowchart TD",
        *flow_body_lines,
    ]
    return "\n".join(document_lines) + "\n"


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_cli_test_")
        self.test_root = Path(self._temp_dir.name)
        self.workspace = self.test_root / "workspace"

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    # ---------------------------------------------------------------- 工具

    def write_linear_flow(self, flow_path: Path) -> None:
        """两任务直线流（无人审）：一次 run 应直接 flow_done。"""
        flow_path.write_text(
            compose_flow_document(
                "cli-linear",
                {"fake-task": fake_agent_command_text("fake_delivery_agent.py")},
                [
                    "  T001[准备数据;;agent=fake-task;;out=data/input.txt] --> T002[生成报告;;agent=fake-task;;out=report/out.txt]",
                ],
            ),
            encoding="utf-8",
        )

    def write_human_gate_flow(self, flow_path: Path) -> None:
        """任务 → 人审 → 收尾：run 应停在人审等待点（退出码 1）。"""
        flow_path.write_text(
            compose_flow_document(
                "cli-human-gate",
                {"fake-task": fake_agent_command_text("fake_delivery_agent.py")},
                [
                    "  T001[准备数据;;agent=fake-task;;out=data/input.txt] --> H001([发布人审])",
                    "  H001 --> T002[打 tag 发布;;agent=fake-task;;out=release-tag.txt]",
                ],
            ),
            encoding="utf-8",
        )

    def write_gate_fail_flow(self, flow_path: Path) -> None:
        """单任务失败注入流：agent 不交付产物且无重试边 → retry 闸触发暂停（退出码 1）。"""
        flow_path.write_text(
            compose_flow_document(
                "cli-gate-fail",
                {
                    "fake-task": fake_agent_command_text(
                        "fake_delivery_agent.py", mode="fail"
                    )
                },
                ["  T001[准备数据;;agent=fake-task;;out=data/input.txt]"],
            ),
            encoding="utf-8",
        )

    def invoke_cli(self, argv: list[str]) -> tuple[int, str, str]:
        """进程内调用 CLI（装配层薄，无全局状态），捕获 stdout/stderr。"""
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout_capture),
            contextlib.redirect_stderr(stderr_capture),
        ):
            exit_code = cli_main(argv)
        return exit_code, stdout_capture.getvalue(), stderr_capture.getvalue()

    # ---------------------------------------------------------------- compile

    def test_compile_good_flow_prints_ir_json(self) -> None:
        flow_path = self.test_root / "linear.mmd"
        self.write_linear_flow(flow_path)
        exit_code, stdout_text, stderr_text = self.invoke_cli(["compile", str(flow_path)])
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr_text, "")
        compiled_ir = json.loads(stdout_text)
        self.assertEqual(compiled_ir["run_name"], "cli-linear")
        self.assertIn("T001", compiled_ir["nodes"])
        self.assertEqual(compiled_ir["nodes"]["T001"]["outputs"], ["data/input.txt"])

    def test_compile_flow_missing_run_reports_line_number_errors(self) -> None:
        flow_path = self.test_root / "bad.mmd"
        flow_path.write_text(
            "---\ndefaults: {agent: fake-task}\nagents:\n  fake-task: {cmd: 'echo hi'}\n---\n"
            "flowchart TD\n  T001[任务;;agent=fake-task;;out=a.txt]\n",
            encoding="utf-8",
        )
        exit_code, stdout_text, stderr_text = self.invoke_cli(["compile", str(flow_path)])
        self.assertEqual(exit_code, 3)
        self.assertIn("line", stderr_text)
        self.assertIn("run", stderr_text)

    def test_compile_syntax_error_reports_line_number(self) -> None:
        flow_path = self.test_root / "broken.mmd"
        flow_path.write_text(
            "---\nrun: broken\n---\nflowchat TD\n  T001[任务]\n",
            encoding="utf-8",
        )
        exit_code, _stdout_text, stderr_text = self.invoke_cli(["compile", str(flow_path)])
        self.assertEqual(exit_code, 3)
        self.assertIn("line 4", stderr_text)

    # ---------------------------------------------------------------- run

    def test_run_linear_flow_completes_with_exit_zero(self) -> None:
        flow_path = self.test_root / "linear.mmd"
        self.write_linear_flow(flow_path)
        exit_code, stdout_text, _stderr_text = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(self.workspace), "--dispatch-timeout", "60"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("flow: done", stdout_text)
        self.assertTrue((self.workspace / "data/input.txt").exists())
        self.assertTrue((self.workspace / "report/out.txt").exists())

    def test_run_stops_at_human_review_with_exit_one(self) -> None:
        flow_path = self.test_root / "human_gate.mmd"
        self.write_human_gate_flow(flow_path)
        exit_code, stdout_text, _stderr_text = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(exit_code, 1)
        self.assertIn("awaiting_human", stdout_text)
        self.assertIn("H001", stdout_text)

    def test_run_generates_agents_stub_in_workspace(self) -> None:
        flow_path = self.test_root / "linear.mmd"
        self.write_linear_flow(flow_path)
        self.invoke_cli(["run", str(flow_path), "--workspace", str(self.workspace)])
        stub_path = self.workspace / "AGENTS.stub.md"
        self.assertTrue(stub_path.exists())
        self.assertIn("data/input.txt", stub_path.read_text(encoding="utf-8"))

    def test_run_writes_flow_paused_banner_with_gate_detail(self) -> None:
        """FIX-C4：闸触发暂停后，todo 头部必须有醒目的 FLOW PAUSED + 闸/恢复提示。"""
        flow_path = self.test_root / "gate_fail.mmd"
        self.write_gate_fail_flow(flow_path)
        run_code, _run_out, _run_err = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(run_code, 1)
        todo_text = (self.workspace / "todo.md").read_text(encoding="utf-8")
        self.assertIn("FLOW PAUSED", todo_text)
        self.assertIn("gate=retry", todo_text)
        self.assertIn("flowrun resume", todo_text)

    # ---------------------------------------------------------------- resume（FIX-C1）

    def test_resume_after_gate_pause_writes_flow_resumed(self) -> None:
        """闸触发挂起 → resume 落 flow_resumed(by=cli) 并解除暂停。"""
        flow_path = self.test_root / "gate_fail.mmd"
        self.write_gate_fail_flow(flow_path)
        run_code, run_stdout, _run_err = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(run_code, 1)
        self.assertIn("flow: paused", run_stdout)

        resume_code, resume_stdout, resume_err = self.invoke_cli(
            ["resume", "--flow", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(resume_code, 0)
        self.assertEqual(resume_err, "")
        self.assertIn("已恢复", resume_stdout)

        ledger_lines = (
            (self.workspace / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        )
        resumed_lines = [line for line in ledger_lines if '"flow_resumed"' in line]
        self.assertEqual(len(resumed_lines), 1, "resume 只落一条 flow_resumed")
        self.assertIn('"by":"cli"', resumed_lines[0])

        _status_code, status_stdout, _status_err = self.invoke_cli(
            ["status", "--workspace", str(self.workspace), "--flow", str(flow_path)]
        )
        self.assertIn("flow: in-progress", status_stdout, "恢复后不再是暂停态")

    def test_resume_enables_progress_to_done(self) -> None:
        """暂停的流经 resume 后能真正推进到 done（不 resume 时 run 只会停在暂停态）。"""
        flow_path = self.test_root / "human_gate.mmd"
        self.write_human_gate_flow(flow_path)
        self.assertEqual(
            self.invoke_cli(["run", str(flow_path), "--workspace", str(self.workspace)])[0],
            1,
        )

        # 用 todo @pause 把流挂起（T002 此时 planned，可被暂停）
        todo_path = self.workspace / "todo.md"
        todo_path.write_text(
            todo_path.read_text(encoding="utf-8").replace(
                "- [ ] T002", "- [ ] T002 @pause"
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            self.invoke_cli(
                ["todo", "--workspace", str(self.workspace), "--flow", str(flow_path)]
            )[0],
            0,
        )
        _paused_code, paused_stdout, _paused_err = self.invoke_cli(
            ["status", "--workspace", str(self.workspace), "--flow", str(flow_path)]
        )
        self.assertIn("flow: paused", paused_stdout)

        # 人审放行 + resume 解除暂停，再 run 应能推进到完成
        self.assertEqual(
            self.invoke_cli(
                ["approve", "H001", "--flow", str(flow_path), "--workspace", str(self.workspace)]
            )[0],
            0,
        )
        self.assertEqual(
            self.invoke_cli(
                ["resume", "--flow", str(flow_path), "--workspace", str(self.workspace)]
            )[0],
            0,
        )
        resume_run_code, resume_run_stdout, _err = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(resume_run_code, 0)
        self.assertIn("flow: done", resume_run_stdout)

    def test_resume_refused_when_not_paused(self) -> None:
        """停在人审等待点（非暂停）时 resume 拒绝：退出码 1，不落 flow_resumed。"""
        flow_path = self.test_root / "human_gate.mmd"
        self.write_human_gate_flow(flow_path)
        self.invoke_cli(["run", str(flow_path), "--workspace", str(self.workspace)])
        resume_code, _resume_out, resume_err = self.invoke_cli(
            ["resume", "--flow", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(resume_code, 1)
        self.assertIn("未处于暂停状态", resume_err)
        ledger_text = (self.workspace / "ledger.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("flow_resumed", ledger_text)

    def test_resume_refused_when_done(self) -> None:
        """已完成的流拒绝 resume：退出码 1。"""
        flow_path = self.test_root / "linear.mmd"
        self.write_linear_flow(flow_path)
        self.assertEqual(
            self.invoke_cli(
                ["run", str(flow_path), "--workspace", str(self.workspace)]
            )[0],
            0,
        )
        resume_code, _resume_out, resume_err = self.invoke_cli(
            ["resume", "--flow", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(resume_code, 1)
        self.assertIn("已完成", resume_err)

    def test_resume_workspace_identity_mismatch_exits_four(self) -> None:
        """工作区账本 run 与图 run 不符 → 退出码 4（工作区身份不匹配）。"""
        linear_flow = self.test_root / "linear.mmd"
        self.write_linear_flow(linear_flow)  # run 名 cli-linear
        self.invoke_cli(["run", str(linear_flow), "--workspace", str(self.workspace)])

        other_flow = self.test_root / "human_gate.mmd"
        self.write_human_gate_flow(other_flow)  # run 名 cli-human-gate
        resume_code, _resume_out, resume_err = self.invoke_cli(
            ["resume", "--flow", str(other_flow), "--workspace", str(self.workspace)]
        )
        self.assertEqual(resume_code, 4)
        self.assertIn("不匹配", resume_err)

    def test_resume_on_missing_workspace_exits_five(self) -> None:
        """工作区未初始化时 resume 退 5（与 status/todo 口径一致）。"""
        flow_path = self.test_root / "linear.mmd"
        self.write_linear_flow(flow_path)
        exit_code, _stdout_text, stderr_text = self.invoke_cli(
            [
                "resume",
                "--flow",
                str(flow_path),
                "--workspace",
                str(self.test_root / "nope"),
            ]
        )
        self.assertEqual(exit_code, 5)
        self.assertIn("工作区未初始化", stderr_text)

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
