"""CLI 装配层测试：compile 好/坏图、run 退出码口径、resume 恢复通道、
approve/deny 幂等与拒绝、status 摘要、todo 子命令（真子进程假 agent，零配置直跑）。

退出码契约：0=done / 1=waiting / 2=halted / 3=编译失败 / 4=工作区身份不匹配 /
5=工作区未初始化 / 6=账本损坏。

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
from flowrun.state.ledger import EventLedger  # noqa: E402

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

        # 用 todo 行尾注释 @pause 把流挂起（T002 此时 planned，可被暂停）
        todo_path = self.workspace / "todo.md"
        todo_path.write_text(
            todo_path.read_text(encoding="utf-8").replace(
                "- [ ] T002", "- [ ] T002 <!-- @pause -->"
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

    def test_resume_on_directory_without_ledger_exits_five(self) -> None:
        """目录存在但缺账本 → 仍是"未初始化"退 5（不因走了内核校验而改口径）。"""
        flow_path = self.test_root / "linear.mmd"
        self.write_linear_flow(flow_path)
        self.workspace.mkdir(parents=True, exist_ok=True)
        exit_code, _stdout_text, stderr_text = self.invoke_cli(
            ["resume", "--flow", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(exit_code, 5)
        self.assertIn("工作区未初始化", stderr_text)

    def test_resume_same_run_name_with_appended_node_exits_four(self) -> None:
        """回归点：同 run 名但图多一个节点 → resume 退 4（改前只比 run 名会放行）。"""
        self.write_workspace_flow_started_ledger("cli-linear", ["T001", "T002"])
        appended_flow = self.test_root / "appended.mmd"
        appended_flow.write_text(
            compose_flow_document(
                "cli-linear",
                {"fake-task": fake_agent_command_text("fake_delivery_agent.py")},
                [
                    "  T001[准备数据;;agent=fake-task;;out=data/input.txt] --> T002[生成报告;;agent=fake-task;;out=report/out.txt]",
                    "  T002 --> T003[额外收尾;;agent=fake-task;;out=extra.txt]",
                ],
            ),
            encoding="utf-8",
        )

        exit_code, _stdout_text, stderr_text = self.invoke_cli(
            ["resume", "--flow", str(appended_flow), "--workspace", str(self.workspace)]
        )
        self.assertEqual(exit_code, 4)
        self.assertIn("不匹配", stderr_text)
        self.assertIn("T003", stderr_text, "诊断应指出多出来的节点")
        self.assertNotIn(
            "flow_resumed",
            (self.workspace / "ledger.jsonl").read_text(encoding="utf-8"),
            "身份不符不得落 flow_resumed",
        )

    def test_resume_same_run_name_with_missing_node_exits_four(self) -> None:
        """回归点：同 run 名但计划节点在当前图中消失 → resume 退 4。"""
        self.write_workspace_flow_started_ledger("cli-linear", ["T001", "T002"])
        reduced_flow = self.test_root / "reduced.mmd"
        reduced_flow.write_text(
            compose_flow_document(
                "cli-linear",
                {"fake-task": fake_agent_command_text("fake_delivery_agent.py")},
                ["  T001[准备数据;;agent=fake-task;;out=data/input.txt]"],
            ),
            encoding="utf-8",
        )

        exit_code, _stdout_text, stderr_text = self.invoke_cli(
            ["resume", "--flow", str(reduced_flow), "--workspace", str(self.workspace)]
        )
        self.assertEqual(exit_code, 4)
        self.assertIn("不匹配", stderr_text)
        self.assertIn("T002", stderr_text, "诊断应指出消失的节点")

    def test_resume_equal_node_set_on_paused_flow_exits_zero(self) -> None:
        """节点集相等 → 身份校验放行；暂停态 resume 正常返回 0 并落 flow_resumed。"""
        flow_path = self.test_root / "gate_fail.mmd"
        self.write_gate_fail_flow(flow_path)
        self.assertEqual(
            self.invoke_cli(["run", str(flow_path), "--workspace", str(self.workspace)])[0],
            1,
        )

        exit_code, stdout_text, stderr_text = self.invoke_cli(
            ["resume", "--flow", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr_text, "")
        self.assertIn("已恢复", stdout_text)
        self.assertIn(
            "flow_resumed",
            (self.workspace / "ledger.jsonl").read_text(encoding="utf-8"),
        )

    def test_resume_legacy_ledger_without_planned_nodes_checks_run_name_only(self) -> None:
        """兼容边界：老账本 flow_started 无 planned_nodes → 只校验 run 名（防误伤）。

        节点集与"计划"不同也不得拦（无 planned_nodes 可比），故身份校验放行后由
        非暂停态退 1 —— 而非身份不匹配的 4。
        """
        self.workspace.mkdir(parents=True, exist_ok=True)
        ledger = EventLedger(self.workspace / "ledger.jsonl")
        try:
            ledger.append(
                "flow_started",
                {"entry_nodes": ["T001"]},  # 老账本：无 planned_nodes
                "cli-linear-flow-started",
                "cli-linear",
            )
        finally:
            ledger.close()

        flow_path = self.test_root / "linear.mmd"
        self.write_linear_flow(flow_path)  # 同 run 名 cli-linear，节点 T001/T002
        exit_code, _stdout_text, stderr_text = self.invoke_cli(
            ["resume", "--flow", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(exit_code, 1, "身份校验放行后由非暂停态退 1")
        self.assertIn("未处于暂停状态", stderr_text)
        self.assertNotIn("不匹配", stderr_text)


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

    # ---------------------------------------------------------------- 账本损坏（退出码 6）

    def corrupt_workspace_ledger_by_appending_garbage(self, flow_path: Path) -> int:
        """先跑一次真流程产出合法账本，再追加一行垃圾制造中段损坏；返回合法行数。"""
        self.invoke_cli(["run", str(flow_path), "--workspace", str(self.workspace)])
        ledger_path = self.workspace / "ledger.jsonl"
        valid_line_count = len(ledger_path.read_text(encoding="utf-8").splitlines())
        with ledger_path.open("a", encoding="utf-8") as ledger_handle:
            ledger_handle.write("{这不是一行合法的 ledger 条目\n")
        return valid_line_count

    def assert_ledger_corruption_reported(self, argv: list[str]) -> None:
        exit_code, _stdout_text, stderr_text = self.invoke_cli(argv)
        self.assertEqual(exit_code, 6, f"{argv[0]} 应以退出码 6 报告账本损坏")
        self.assertNotIn("Traceback", stderr_text, "账本损坏不得打 Python traceback")
        self.assertIn("账本损坏", stderr_text)
        self.assertIn("ledger.jsonl", stderr_text, "诊断必须指出是哪个账本文件")

    def test_ledger_corruption_reports_exit_six_across_all_subcommands(self) -> None:
        """回归点：账本中段损坏时 run/status/todo/resume/approve 一律退 6 且无 traceback。"""
        flow_path = self.test_root / "human_gate.mmd"
        self.write_human_gate_flow(flow_path)
        self.corrupt_workspace_ledger_by_appending_garbage(flow_path)
        workspace_text = str(self.workspace)
        for argv in (
            ["status", "--workspace", workspace_text, "--flow", str(flow_path)],
            ["todo", "--workspace", workspace_text, "--flow", str(flow_path)],
            ["resume", "--flow", str(flow_path), "--workspace", workspace_text],
            ["approve", "H001", "--flow", str(flow_path), "--workspace", workspace_text],
            ["run", str(flow_path), "--workspace", workspace_text],
        ):
            with self.subTest(subcommand=argv[0]):
                self.assert_ledger_corruption_reported(argv)

    def test_snapshot_integrity_failure_also_reports_exit_six(self) -> None:
        """快照恢复路径的损坏信号与账本解析失败同型（LedgerCorruptionError → 退 6）。

        快照点 head_hash 与账本重算哈希不符 ⇒ recover_flow_state 的防篡改核对
        抛 LedgerCorruptionError（不是另一种异常）——故 main 的单一捕获即可覆盖。
        """
        flow_path = self.test_root / "linear.mmd"
        self.write_linear_flow(flow_path)
        self.invoke_cli(["run", str(flow_path), "--workspace", str(self.workspace)])
        ledger_line_count = len(
            (self.workspace / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        )
        snapshots_directory = self.workspace / "snapshots"
        snapshots_directory.mkdir(parents=True, exist_ok=True)
        (snapshots_directory / f"snapshot_{ledger_line_count:010d}.json").write_text(
            json.dumps(
                {
                    "seq": ledger_line_count,
                    "head_hash": "0" * 64,  # 与账本真实哈希不符：伪造/损坏的快照
                    "created_ts": 0,
                    "state": {},
                }
            ),
            encoding="utf-8",
        )

        exit_code, _stdout_text, stderr_text = self.invoke_cli(
            ["status", "--workspace", str(self.workspace)]
        )
        self.assertEqual(exit_code, 6)
        self.assertNotIn("Traceback", stderr_text)
        self.assertIn("账本损坏", stderr_text)
        self.assertIn("ledger.jsonl", stderr_text)

    def test_snapshot_record_missing_state_reports_exit_six(self) -> None:
        """形状非法（缺 state 键）的快照记录经 CLI 退 6，无 traceback 且含可读原因。

        改前 recover_flow_state 直接 ``latest_record["state"]`` 抛裸 KeyError，
        穿透 main() 打 traceback（违反退出码 6 契约）。
        """
        flow_path = self.test_root / "linear.mmd"
        self.write_linear_flow(flow_path)
        self.invoke_cli(["run", str(flow_path), "--workspace", str(self.workspace)])
        ledger_line_count = len(
            (self.workspace / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        )
        snapshots_directory = self.workspace / "snapshots"
        snapshots_directory.mkdir(parents=True, exist_ok=True)
        snapshot_path = snapshots_directory / f"snapshot_{ledger_line_count:010d}.json"
        snapshot_path.write_text(
            json.dumps(
                {"seq": ledger_line_count, "head_hash": "", "created_ts": 0}  # 缺 state
            ),
            encoding="utf-8",
        )

        exit_code, _stdout_text, stderr_text = self.invoke_cli(
            ["status", "--workspace", str(self.workspace)]
        )
        self.assertEqual(exit_code, 6)
        self.assertNotIn("Traceback", stderr_text)
        self.assertIn("账本损坏", stderr_text)
        self.assertIn("快照记录形状非法", stderr_text)
        self.assertIn(str(snapshot_path), stderr_text)


    # ---------------------------------------------------------------- 身份校验先于写入

    def write_workspace_flow_started_ledger(
        self, run_name: str, planned_node_ids: list[str]
    ) -> None:
        """只落一条 flow_started 的干净工作区（无 stub / 无 packets·audit·snapshots）。"""
        self.workspace.mkdir(parents=True, exist_ok=True)
        ledger = EventLedger(self.workspace / "ledger.jsonl")
        try:
            ledger.append(
                "flow_started",
                {"entry_nodes": planned_node_ids[:1], "planned_nodes": planned_node_ids},
                f"{run_name}-flow-started",
                run_name,
            )
        finally:
            ledger.close()

    def assert_workspace_left_untouched(
        self, exit_code: int, stderr_text: str, expected_ledger_line_count: int
    ) -> None:
        """身份不符的 run 不得在工作区留任何痕迹（账本 / stub / 内核目录）。"""
        self.assertEqual(exit_code, 4)
        self.assertIn("不匹配", stderr_text)
        ledger_lines = (
            (self.workspace / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        )
        self.assertEqual(
            len(ledger_lines),
            expected_ledger_line_count,
            "校验失败不得向工作区账本落任何事件",
        )
        self.assertFalse((self.workspace / "AGENTS.stub.md").exists())
        for kernel_directory_name in ("audit", "packets", "snapshots"):
            self.assertFalse(
                (self.workspace / kernel_directory_name).exists(),
                f"校验失败不得新建 {kernel_directory_name}/",
            )

    def test_run_with_foreign_run_name_leaves_workspace_untouched(self) -> None:
        """回归点：拿 B 的流程跑 A 的工作区，退 4 且 A 的账本/stub/目录零变化。"""
        foreign_flow = self.test_root / "human_gate.mmd"
        self.write_human_gate_flow(foreign_flow)  # run 名 cli-human-gate
        self.write_workspace_flow_started_ledger("cli-linear", ["T001", "T002"])
        # 预置一份带用户标记的 todo.md：B 的流程若被推进，标记会被落成 run='cli-human-gate'
        # 的 user_directive 污染 A 的账本（修复前的实际现象）。
        todo_path = self.workspace / "todo.md"
        user_todo_text = "- [ ] T002 <!-- @pause -->\n"
        todo_path.write_text(user_todo_text, encoding="utf-8")

        exit_code, _stdout_text, stderr_text = self.invoke_cli(
            ["run", str(foreign_flow), "--workspace", str(self.workspace)]
        )
        self.assert_workspace_left_untouched(
            exit_code, stderr_text, expected_ledger_line_count=1
        )
        self.assertEqual(
            todo_path.read_text(encoding="utf-8"),
            user_todo_text,
            "身份不符不得改写用户已有的 todo.md",
        )

    def test_run_with_appended_node_flow_leaves_workspace_untouched(self) -> None:
        """回归点：同 run 名但图多一个节点（追加），退 4 且工作区零变化。"""
        extended_flow = self.test_root / "linear_extended.mmd"
        extended_flow.write_text(
            compose_flow_document(
                "cli-linear",
                {"fake-task": fake_agent_command_text("fake_delivery_agent.py")},
                [
                    "  T001[准备数据;;agent=fake-task;;out=data/input.txt] --> T002[生成报告;;agent=fake-task;;out=report/out.txt]",
                    "  T002 --> T003[额外收尾;;agent=fake-task;;out=extra.txt]",
                ],
            ),
            encoding="utf-8",
        )
        self.write_workspace_flow_started_ledger("cli-linear", ["T001", "T002"])

        exit_code, _stdout_text, stderr_text = self.invoke_cli(
            ["run", str(extended_flow), "--workspace", str(self.workspace)]
        )
        self.assert_workspace_left_untouched(
            exit_code, stderr_text, expected_ledger_line_count=1
        )
        self.assertFalse((self.workspace / "todo.md").exists())
        self.assertIn("T003", stderr_text, "诊断应指出多出来的节点")

    def test_run_still_cold_starts_on_empty_workspace(self) -> None:
        """回归点：身份校验前移不得误伤冷启动（空账本/不存在的目录仍能跑）。"""
        flow_path = self.test_root / "linear.mmd"
        self.write_linear_flow(flow_path)
        exit_code, _stdout_text, _stderr_text = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue((self.workspace / "AGENTS.stub.md").exists())


if __name__ == "__main__":
    unittest.main()
