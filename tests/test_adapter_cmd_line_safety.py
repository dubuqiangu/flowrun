"""cmd_line_safety.py 测试：缺陷 1（path 模式不中和 %VAR%）与缺陷 2（cmd 垫片参数级注入）。

两条缺陷共享同一条"失败关闭"规则：要嵌进 cmd.exe 命令行的参数含 ``%`` / ``"`` /
CR / LF 时一律**拒绝执行**（``%`` 即使加双引号也会被 cmd.exe 展开，硬拼转义不可靠）。
缺陷 2 另要求每个参数**无条件**加双引号，让 ``&`` / ``|`` / ``^`` / ``<`` / ``>``
在引号内失活。边界：直接 ``CreateProcess`` 的进程（不经 cmd.exe）不做此校验——
那里 ``%`` 是字面量，误伤反而是缺陷。
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from flowrun.adapters.base import plan_windows_command_launch  # noqa: E402
from flowrun.adapters.cmd_line_safety import (  # noqa: E402
    CmdExeUnsafeArgumentError,
    render_cmd_exe_arguments,
    require_cmd_exe_safe_argument,
)

IS_WINDOWS = sys.platform == "win32"


class RenderCmdExeArgumentsTest(unittest.TestCase):
    """缺陷 2：每个参数无条件双引号包裹，cmd 元字符在引号内失活。"""

    def test_every_argument_is_quoted_unconditionally(self):
        self.assertEqual(render_cmd_exe_arguments(["a", "b"]), '"a" "b"')

    def test_spaced_path_is_quoted(self):
        self.assertEqual(
            render_cmd_exe_arguments([r"C:\dir with space\agent.cmd"]),
            '"C:\\dir with space\\agent.cmd"',
        )

    def test_bare_metacharacter_arguments_are_quoted(self):
        # list2cmdline 在参数不含空白/引号时不加引号 -> 裸 & / | / ^ / < > 会被 cmd 重分词。
        self.assertEqual(render_cmd_exe_arguments(["a&b"]), '"a&b"')
        self.assertEqual(render_cmd_exe_arguments(["a|b"]), '"a|b"')
        self.assertEqual(render_cmd_exe_arguments(["a^b"]), '"a^b"')
        self.assertEqual(render_cmd_exe_arguments(["a<b>c"]), '"a<b>c"')

    def test_empty_argument_renders_as_empty_quotes(self):
        self.assertEqual(render_cmd_exe_arguments([""]), '""')
        self.assertEqual(render_cmd_exe_arguments(["a", "", "b"]), '"a" "" "b"')


class CmdExeUnsafeArgumentRejectionTest(unittest.TestCase):
    """缺陷 1/2 共享规则：含 % / " / CR / LF 的参数失败关闭，并给可执行建议。"""

    def test_argument_with_percent_is_rejected_with_actionable_advice(self):
        with self.assertRaises(CmdExeUnsafeArgumentError) as raised_context:
            require_cmd_exe_safe_argument("100%done")
        error_message = str(raised_context.exception)
        self.assertIn("%", error_message)
        self.assertIn("工作区", error_message)

    def test_argument_with_double_quote_is_rejected(self):
        with self.assertRaises(CmdExeUnsafeArgumentError):
            require_cmd_exe_safe_argument('a"b')

    def test_argument_with_newline_or_carriage_return_is_rejected(self):
        with self.assertRaises(CmdExeUnsafeArgumentError):
            require_cmd_exe_safe_argument("a\nb")
        with self.assertRaises(CmdExeUnsafeArgumentError):
            require_cmd_exe_safe_argument("a\rb")

    def test_render_rejects_unsafe_argument_among_safe_ones(self):
        with self.assertRaises(CmdExeUnsafeArgumentError):
            render_cmd_exe_arguments(["safe", "a%b"])

    def test_plain_metacharacter_argument_is_safe_but_gets_quoted(self):
        # & 本身不是"不可表达"，只是必须加引号（不能在 render 之外裸拼）。
        require_cmd_exe_safe_argument("a&b")
        require_cmd_exe_safe_argument(r"C:\plain\ok path\agent.cmd")


class BatchShimArgumentRenderingTest(unittest.TestCase):
    """缺陷 1/2 集成：.cmd/.bat 分支经 render_cmd_exe_arguments（替换 list2cmdline）。"""

    def setUp(self):
        if not IS_WINDOWS:
            self.skipTest("Windows 专属行为")
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="flowrun_cmd_line_safety_"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp_dir, ignore_errors=True))
        (self.tmp_dir / "fake_agent.cmd").write_text("@echo off\r\n", encoding="ascii")

    def test_batch_branch_quotes_every_argument(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp_dir)}):
            launch_plan = plan_windows_command_launch(["fake_agent", "a&b", "--flag"])
        self.assertIsNone(launch_plan.exec_arguments)
        self.assertTrue(
            launch_plan.shell_command_line.endswith('"a&b" "--flag"'),
            launch_plan.shell_command_line,
        )

    def test_batch_branch_rejects_percent_in_packet_path(self):
        # 缺陷 1：path 模式把 packet 路径放进命令行；路径含 % 时 cmd.exe 会展开它。
        unsafe_packet_path = self.tmp_dir / "100%" / "packet.md"
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp_dir)}):
            with self.assertRaises(CmdExeUnsafeArgumentError) as raised_context:
                plan_windows_command_launch(["fake_agent", str(unsafe_packet_path)])
        self.assertIn("%", str(raised_context.exception))

    def test_batch_branch_rejects_double_quote_in_argument(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp_dir)}):
            with self.assertRaises(CmdExeUnsafeArgumentError):
                plan_windows_command_launch(["fake_agent", 'a"b'])


class DirectExecIsNotRestrictedTest(unittest.TestCase):
    """边界：不经 cmd.exe 的进程（直接 CreateProcess / execvp）不做 % 校验。"""

    def test_direct_exec_argument_with_percent_is_passed_through(self):
        launch_plan = plan_windows_command_launch(
            [sys.executable, "-c", "pass", "100%done"]
        )
        self.assertIsNone(launch_plan.shell_command_line)
        self.assertEqual(list(launch_plan.exec_arguments)[-1], "100%done")

    def test_ps1_shim_path_with_percent_is_not_rejected(self):
        # .ps1 经 powershell -File（CreateProcess）启动，% 是字面量，不能误伤。
        if not IS_WINDOWS:
            self.skipTest("Windows 专属行为")
        tmp_dir = Path(tempfile.mkdtemp(prefix="flowrun_cmd_line_safety_ps1_"))
        self.addCleanup(lambda: shutil.rmtree(tmp_dir, ignore_errors=True))
        (tmp_dir / "ps_agent.ps1").write_text("Write-Output ok\n", encoding="ascii")
        with mock.patch.dict(os.environ, {"PATH": str(tmp_dir),
                                          "PATHEXT": ".COM;.EXE;.CMD;.PS1"}):
            launch_plan = plan_windows_command_launch(["ps_agent", "100%done"])
        self.assertIsNone(launch_plan.shell_command_line)
        self.assertIn("100%done", launch_plan.exec_arguments)


if __name__ == "__main__":
    unittest.main()
