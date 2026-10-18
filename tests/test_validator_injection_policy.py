"""编译期注入策略校验测试：已知 shell 命令头 + content 注入必须**在编译期**被拒。

背景：注入模式是**派生**的——命令头解析到 ``.cmd/.bat/.ps1`` 垫片走 path 注入，
其余走 content 注入（packet 正文进 argv）。若命令头本身就是 shell
（cmd / powershell / pwsh / sh / bash），content 模式等于把 packet 正文交给该
shell 重新分词（重定向/管道/命令分隔符）。前端**没有任何开关**能配置注入模式，
所以这条策略只能在编译期把关。

修复前的状态：判定函数（``adapters/registry.py``）只有入口、**没有接进编译
路径**，产品实际不会拒绝。本文件钉住"编译期真的拒绝"，并覆盖
"拒绝发生在任何账本写入之前"（fail-before-write）。

直跑：python -m unittest discover -s tests
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
from flowrun.compiler.validator import (  # noqa: E402
    FlowValidationError,
    compile_flow_source,
)

SHELL_AGENT_BLOCK = "  shell-task: {cmd: 'cmd /c echo hi'}\n"
SAFE_AGENT_BLOCK = "  plain-task: {cmd: 'echo hi'}\n"


def compose_flow_source(run_name: str, agents_block: str, default_agent: str) -> str:
    """一份最小可编译的直线流；``agents_block`` 是 frontmatter ``agents:`` 下的条目。"""
    return (
        "---\n"
        f"run: {run_name}\n"
        f"defaults: {{agent: {default_agent}, budget_tokens: 1000}}\n"
        "agents:\n"
        f"{agents_block}"
        "---\n"
        "flowchart TD\n"
        f"  T001[准备数据;;agent={default_agent};;out=stage-out.txt]\n"
    )


class ValidatorInjectionPolicyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_injection_policy_")
        self.test_root = Path(self._temp_dir.name)
        self.workspace = self.test_root / "workspace"

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def invoke_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout_capture),
            contextlib.redirect_stderr(stderr_capture),
        ):
            exit_code = cli_main(argv)
        return exit_code, stdout_capture.getvalue(), stderr_capture.getvalue()

    # ------------------------------------------------------- 校验器层

    def test_shell_command_head_with_content_injection_is_rejected(self) -> None:
        source = compose_flow_source(
            "injection-shell-head", SHELL_AGENT_BLOCK, "shell-task"
        )

        with self.assertRaises(FlowValidationError) as raised_context:
            compile_flow_source(source)

        message = str(raised_context.exception)
        self.assertIn("shell-task", message)
        self.assertIn("shell", message)
        self.assertIn("content", message)

    def test_non_shell_command_head_still_compiles(self) -> None:
        source = compose_flow_source(
            "injection-plain-head", SAFE_AGENT_BLOCK, "plain-task"
        )

        flow_ir = compile_flow_source(source)

        self.assertIn("plain-task", flow_ir.agent_registry)

    def test_every_violating_agent_is_reported_in_one_pass(self) -> None:
        agents_block = SHELL_AGENT_BLOCK + "  bash-task: {cmd: 'bash -c echo hi'}\n"
        source = compose_flow_source(
            "injection-two-shells", agents_block, "shell-task"
        )

        with self.assertRaises(FlowValidationError) as raised_context:
            compile_flow_source(source)

        findings = [str(finding) for finding in raised_context.exception.errors]
        self.assertEqual(len(findings), 2, findings)
        self.assertTrue(any("shell-task" in finding for finding in findings))
        self.assertTrue(any("bash-task" in finding for finding in findings))

    def test_explicit_shim_suffix_head_is_not_treated_as_shell(self) -> None:
        """显式写垫片后缀（.cmd）→ 确定走 path 注入，不该被 shell 名单误伤。"""
        source = compose_flow_source(
            "injection-shim-suffix", "  shim-task: {cmd: 'bash.cmd -p x'}\n", "shim-task"
        )

        flow_ir = compile_flow_source(source)

        self.assertIn("shim-task", flow_ir.agent_registry)

    # ------------------------------------------------------- CLI 层

    def test_compile_subcommand_reports_rejection(self) -> None:
        flow_path = self.test_root / "shell_head.mmd"
        flow_path.write_text(
            compose_flow_source(
                "injection-cli-compile", SHELL_AGENT_BLOCK, "shell-task"
            ),
            encoding="utf-8",
        )

        exit_code, _stdout_text, stderr_text = self.invoke_cli(
            ["compile", str(flow_path)]
        )

        self.assertEqual(exit_code, 3, "编译期拒绝沿用退出码 3（编译·校验）")
        self.assertNotIn("Traceback", stderr_text)
        self.assertIn("shell-task", stderr_text)

    def test_run_refuses_before_any_ledger_write(self) -> None:
        flow_path = self.test_root / "shell_head_run.mmd"
        flow_path.write_text(
            compose_flow_source("injection-cli-run", SHELL_AGENT_BLOCK, "shell-task"),
            encoding="utf-8",
        )

        exit_code, _stdout_text, stderr_text = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(self.workspace)]
        )

        self.assertEqual(exit_code, 3)
        self.assertNotIn("Traceback", stderr_text)
        self.assertIn("shell-task", stderr_text)
        self.assertFalse(
            (self.workspace / "ledger.jsonl").exists(),
            "编译期拒绝必须发生在任何账本写入之前",
        )


if __name__ == "__main__":
    unittest.main()
