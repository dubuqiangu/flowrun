"""CLI 装配层测试共享支撑：路径引导、假 agent 命令模板、flow.mmd 拼装、进程内 CLI 基类。

被 test_cli*.py 复用（文件名不带 test_ 前缀，避免被 unittest 收集）。
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

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


class CliHarnessTestCase(unittest.TestCase):
    """进程内调用 CLI 的公共夹具：临时工作区 + 常用流程文本 + stdout/stderr 捕获。"""

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
