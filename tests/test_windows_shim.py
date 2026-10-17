"""Windows 垫片解析测试：npm 风格 .cmd/.bat/.ps1 垫片经 cmd /c 或 powershell 启动。

真机取证：Windows 上 `opencode` 解析为 opencode.ps1 / opencode.cmd 垫片，
shutil.which 找得到（probe 通过）但 CreateProcess 无法直接 exec（spawn 报
WinError 2），必须在适配层做垫片解析。非 Windows 平台本文件整体跳过。
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from flowrun.adapters.base import (  # noqa: E402
    DispatchResult,
    CommandHeadUnresolvableError,
    StreamMonitoringAdapter,
    plan_windows_command_launch,
    resolve_injection_mode,
)
from flowrun.adapters.registry import resolve_command_path  # noqa: E402

IS_WINDOWS = sys.platform == "win32"
ECHO_ARGUMENTS_SHIM_BODY = "@echo off\r\necho SHIM_ARGS:%*\r\nexit /b 0\r\n"


class PlanWindowsCommandLaunchTest(unittest.TestCase):
    """纯函数单测：垫片识别、启动方式与"不可解析"错误。"""

    def setUp(self):
        if not IS_WINDOWS:
            self.skipTest("Windows 专属行为")
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="flowrun_shim_"))
        self.addCleanup(lambda: _remove_tree_quietly(self.tmp_dir))

    def _write_shim(self, filename: str) -> Path:
        shim_path = self.tmp_dir / filename
        shim_path.write_text("@echo off\r\n", encoding="ascii")
        return shim_path

    def test_cmd_shim_planned_as_cmd_shell_command_line(self):
        self._write_shim("fake_agent.cmd")
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp_dir)}):
            launch_plan = plan_windows_command_launch(
                ["fake_agent", "-p", "--packet", "x.md"]
            )
        # .cmd 垫片只能经 cmd.exe 的整条命令行启动（argv 形态会被 cmd 重新分词）
        self.assertIsNone(launch_plan.exec_arguments)
        # 盘上大小写可能不同（which/字面探测保留实际文件名），比较不区分大小写
        self.assertIn("fake_agent.cmd", launch_plan.shell_command_line.casefold())
        self.assertTrue(
            launch_plan.shell_command_line.endswith("-p --packet x.md"),
            launch_plan.shell_command_line,
        )

    def test_bat_shim_planned_as_cmd_shell_command_line(self):
        self._write_shim("fake_agent.bat")
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp_dir)}):
            launch_plan = plan_windows_command_launch(["fake_agent"])
        self.assertIsNone(launch_plan.exec_arguments)
        self.assertIn("fake_agent.bat", launch_plan.shell_command_line.casefold())

    def test_ps1_shim_planned_through_powershell(self):
        ps1_path = self.tmp_dir / "fake_agent.ps1"
        ps1_path.write_text("Write-Output ok\n", encoding="ascii")
        # 默认 PATHEXT 不含 .PS1，须显式补上（真机 opencode.ps1 场景：npm 垫片通常同时有 .cmd）。
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp_dir),
                                          "PATHEXT": ".COM;.EXE;.CMD;.PS1"}):
            launch_plan = plan_windows_command_launch(["fake_agent"])
        self.assertIsNone(launch_plan.shell_command_line)
        self.assertEqual(launch_plan.exec_arguments[0], "powershell")
        self.assertIn("-File", launch_plan.exec_arguments)
        self.assertEqual(
            str(Path(launch_plan.exec_arguments[-1])).casefold(), str(ps1_path).casefold()
        )

    def test_non_shim_command_head_is_passed_through_unchanged(self):
        launch_plan = plan_windows_command_launch(
            [sys.executable, "-m", "flowrun_fake_agent"]
        )
        self.assertIsNone(launch_plan.shell_command_line)
        self.assertEqual(
            list(launch_plan.exec_arguments),
            [sys.executable, "-m", "flowrun_fake_agent"],
        )

    def test_empty_command_yields_empty_exec_arguments(self):
        launch_plan = plan_windows_command_launch([])
        self.assertEqual(launch_plan.exec_arguments, ())
        self.assertIsNone(launch_plan.shell_command_line)


class WindowsLaunchPlanTest(unittest.TestCase):
    """启动计划的形状约束：exec argv 与 shell 命令行必须二选一。"""

    def test_plan_rejects_both_forms_and_neither_form(self):
        from flowrun.adapters.base import WindowsLaunchPlan

        with self.assertRaises(ValueError):
            WindowsLaunchPlan(exec_arguments=("a",), shell_command_line="a")
        with self.assertRaises(ValueError):
            WindowsLaunchPlan()
        self.assertEqual(WindowsLaunchPlan(exec_arguments=()).exec_arguments, ())


class DispatchThroughCmdShimTest(unittest.TestCase):
    """集成：经 .cmd 垫片真实拉起子进程走完双信号监视器。"""

    def setUp(self):
        if not IS_WINDOWS:
            self.skipTest("Windows 专属行为")
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="flowrun_shim_dispatch_"))
        self.addCleanup(lambda: _remove_tree_quietly(self.tmp_dir))
        self.packet_path = self.tmp_dir / "task.packet.md"
        self.packet_path.write_text("# 任务\n做点事\n", encoding="utf-8")
        # 垫片脚本：回显一行并正常退出（模拟 agent 完成退出）。
        shim_name = "fake_echo_agent.cmd"
        (self.tmp_dir / shim_name).write_text(
            "@echo off\r\necho agent-finished\r\nexit /b 0\r\n", encoding="ascii"
        )

    def test_dispatch_runs_cmd_shim_to_completion(self):
        async def run_dispatch():
            adapter = StreamMonitoringAdapter()
            with mock.patch.dict(os.environ, {"PATH": str(self.tmp_dir)}):
                return await adapter.dispatch(
                    packet_path=self.packet_path,
                    agent_command=["fake_echo_agent"],
                    work_dir=self.tmp_dir,
                    timeout_seconds=10.0,
                )

        result = asyncio.run(run_dispatch())
        self.assertIsInstance(result, DispatchResult)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.timed_out)
        self.assertTrue(self.packet_path.exists())


class DispatchThroughSpacedShimTest(unittest.TestCase):
    """C2 回归：垫片路径与追加参数**都含空格**时垫片完全不执行。

    旧实现把 ``cmd /c <shim> <args>`` 交给 list2cmdline，得到 4 个引号，
    触发 cmd.exe 的引号剥离启发式（示例性实测：rc=1、stdout 为空、
    stderr 是 "'C:\\Users\\...\\flowrun' 不是内部或外部命令"）。
    这里三条组合都覆盖：两侧含空格 / 仅垫片含空格 / 都不含空格。
    """

    def setUp(self):
        if not IS_WINDOWS:
            self.skipTest("Windows 专属行为")
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="flowrun_shim_spaces_"))
        self.addCleanup(lambda: _remove_tree_quietly(self.tmp_dir))
        self.spaced_dir = self.tmp_dir / "dir with space"
        self.spaced_dir.mkdir()
        self.plain_dir = self.tmp_dir / "plain"
        self.plain_dir.mkdir()
        for shim_directory in (self.spaced_dir, self.plain_dir):
            (shim_directory / "echo_agent.cmd").write_text(
                ECHO_ARGUMENTS_SHIM_BODY, encoding="ascii"
            )

    def _dispatch_through_shim(self, shim_directory: Path,
                               packet_directory: Path) -> tuple[DispatchResult, Path]:
        packet_path = packet_directory / "task packet.md"
        packet_path.write_text("# 任务\n做点事\n", encoding="utf-8")

        async def run_dispatch():
            adapter = StreamMonitoringAdapter()
            with mock.patch.dict(os.environ, {"PATH": str(shim_directory)}):
                return await adapter.dispatch(
                    packet_path=packet_path,
                    agent_command=["echo_agent"],
                    work_dir=self.tmp_dir,
                    timeout_seconds=10.0,
                )

        return asyncio.run(run_dispatch()), packet_path.resolve()

    def _assert_placeholder_expanded(self, shim_directory: Path,
                                     packet_directory: Path) -> None:
        result, expected_packet_path = self._dispatch_through_shim(
            shim_directory, packet_directory
        )
        stdout_audit = result.stdout_path.read_text(encoding="utf-8", errors="replace")
        self.assertEqual(result.exit_code, 0, f"stdout={stdout_audit!r}")
        self.assertFalse(result.timed_out)
        self.assertIn("SHIM_ARGS:", stdout_audit)
        # 垫片确实收到并展开出 packet 绝对路径（agent 自己读文件）
        self.assertIn(str(expected_packet_path), stdout_audit)

    def test_spaced_shim_path_with_spaced_argument_runs(self):
        self._assert_placeholder_expanded(self.spaced_dir, self.spaced_dir)

    def test_spaced_shim_path_with_plain_argument_runs(self):
        self._assert_placeholder_expanded(self.spaced_dir, self.plain_dir)

    def test_plain_shim_path_with_plain_argument_runs(self):
        self._assert_placeholder_expanded(self.plain_dir, self.plain_dir)


class BatchShimInjectionSafetyTest(unittest.TestCase):
    """FIX-1：垫片（.cmd/.bat）路径下必须投递 packet **路径**，禁止内容注入。

    原因：``cmd /c shim <args>`` 的命令行会被 cmd.exe 重新分词，packet 正文里的
    ``"`` 与 ``&`` 会被当成命令分隔符执行（任意命令注入）。垫片下真 agent 需要
    读文件，本来就只需要路径。
    """

    DANGEROUS_MARKER = "PWNED_MARKER"

    def setUp(self):
        if not IS_WINDOWS:
            self.skipTest("Windows 专属行为")
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="flowrun_shim_injection_"))
        self.addCleanup(lambda: _remove_tree_quietly(self.tmp_dir))
        # 垫片回显自己收到的 argv，便于断言"agent 收到的是路径"。
        (self.tmp_dir / "fake_argv_agent.cmd").write_text(
            "@echo off\r\necho SHIM_ARGS:%*\r\nexit /b 0\r\n", encoding="ascii"
        )
        self.packet_path = self.tmp_dir / "task.packet.md"
        self.packet_path.write_text(
            f'do work " & echo {self.DANGEROUS_MARKER} & " tail', encoding="utf-8"
        )

    def test_batch_shim_receives_packet_path_and_cannot_be_injected(self):
        async def run_dispatch():
            adapter = StreamMonitoringAdapter()
            with mock.patch.dict(os.environ, {"PATH": str(self.tmp_dir)}):
                return await adapter.dispatch(
                    packet_path=self.packet_path,
                    agent_command=["fake_argv_agent"],
                    work_dir=self.tmp_dir,
                    timeout_seconds=10.0,
                )

        result = asyncio.run(run_dispatch())
        stdout_audit = result.stdout_path.read_text(encoding="utf-8", errors="replace")
        # 危险标记绝不能被 cmd.exe 当成第二条命令执行。
        self.assertNotIn(self.DANGEROUS_MARKER, stdout_audit)
        # 垫片拿到的是 packet 文件绝对路径（agent 自己读文件）。
        self.assertIn(str(self.packet_path.resolve()), stdout_audit)


class WindowsCommandHeadResolutionTest(unittest.TestCase):
    """C3：.ps1 也是垫片；PATHEXT 漏掉 .PS1 时有界兜底；解析不到时给可读错误。"""

    def setUp(self):
        if not IS_WINDOWS:
            self.skipTest("Windows 专属行为")
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="flowrun_shim_resolve_"))
        self.addCleanup(lambda: _remove_tree_quietly(self.tmp_dir))
        (self.tmp_dir / "ps_only_agent.ps1").write_text(
            "Write-Output ok\n", encoding="ascii"
        )
        self.packet_path = self.tmp_dir / "task.packet.md"
        self.packet_path.write_text("# 任务\n", encoding="utf-8")

    def test_pathext_including_ps1_uses_path_injection_and_powershell(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp_dir),
                                          "PATHEXT": ".COM;.EXE;.CMD;.PS1"}):
            self.assertEqual(resolve_injection_mode(["ps_only_agent"]), "path")
            launch_plan = plan_windows_command_launch(["ps_only_agent", "-p"])
        # 不变式：算垫片 ⇒ path 注入 ∧ 经 powershell 启动（两边判定必须一致）
        self.assertIsNone(launch_plan.shell_command_line)
        self.assertEqual(launch_plan.exec_arguments[0], "powershell")
        self.assertIn("-File", launch_plan.exec_arguments)

    def test_pathext_without_ps1_still_resolves_the_ps1_shim(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp_dir),
                                          "PATHEXT": ".COM;.EXE;.CMD"}):
            resolved_path = resolve_command_path("ps_only_agent")
            self.assertIsNotNone(resolved_path)
            self.assertEqual(Path(resolved_path).suffix.lower(), ".ps1")
            self.assertEqual(resolve_injection_mode(["ps_only_agent"]), "path")
            launch_plan = plan_windows_command_launch(["ps_only_agent"])
        self.assertEqual(launch_plan.exec_arguments[0], "powershell")

    def test_unresolvable_command_head_raises_readable_error(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp_dir),
                                          "PATHEXT": ".COM;.EXE"}):
            self.assertIsNone(resolve_command_path("ghost_agent"))
            self.assertEqual(resolve_injection_mode(["ghost_agent"]), "content")
            with self.assertRaises(CommandHeadUnresolvableError) as raised_context:
                plan_windows_command_launch(["ghost_agent", "run"])
        error_message = str(raised_context.exception)
        self.assertIn("ghost_agent", error_message)
        self.assertIn("不可解析", error_message)

    def test_dispatch_reports_readable_error_instead_of_winerror2(self):
        async def run_dispatch():
            adapter = StreamMonitoringAdapter()
            with mock.patch.dict(os.environ, {"PATH": str(self.tmp_dir),
                                              "PATHEXT": ".COM;.EXE"}):
                return await adapter.dispatch(
                    packet_path=self.packet_path,
                    agent_command=["ghost_agent"],
                    work_dir=self.tmp_dir,
                    timeout_seconds=10.0,
                )

        with self.assertRaises(CommandHeadUnresolvableError) as raised_context:
            asyncio.run(run_dispatch())
        self.assertIn("ghost_agent", str(raised_context.exception))


class ProcessTreeKillTest(unittest.TestCase):
    """FIX-2：超时 kill 必须连坐孙进程（垫片下真 agent 是 cmd 的孙进程）。"""

    GRANDCHILD_DELAY_SECONDS = 3.0
    POST_KILL_WAIT_SECONDS = 4.0

    def setUp(self):
        if not IS_WINDOWS:
            self.skipTest("Windows 专属行为")
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="flowrun_shim_tree_kill_"))
        self.addCleanup(lambda: _remove_tree_quietly(self.tmp_dir))
        self.marker_path = self.tmp_dir / "grandchild.marker"
        self.child_script_path = FIXTURES_DIR / "slow_marker_writer.py"
        (self.tmp_dir / "fake_slow_agent.cmd").write_text(
            "@echo off\r\n"
            f'"{sys.executable}" "{self.child_script_path}" '
            f'{self.GRANDCHILD_DELAY_SECONDS} "{self.marker_path}"\r\n'
            "exit /b 0\r\n",
            encoding="ascii",
        )
        self.packet_path = self.tmp_dir / "task.packet.md"
        self.packet_path.write_text("# 任务\n慢活\n", encoding="utf-8")

    def test_timeout_kill_terminates_grandchild_process(self):
        async def run_dispatch():
            adapter = StreamMonitoringAdapter()
            with mock.patch.dict(os.environ, {"PATH": str(self.tmp_dir)}):
                return await adapter.dispatch(
                    packet_path=self.packet_path,
                    agent_command=["fake_slow_agent"],
                    work_dir=self.tmp_dir,
                    timeout_seconds=0.5,
                )

        result = asyncio.run(run_dispatch())
        self.assertTrue(result.timed_out)
        time.sleep(self.POST_KILL_WAIT_SECONDS)
        self.assertFalse(
            self.marker_path.exists(),
            "孙进程在父进程被杀后仍在写 workspace（kill 未连坐进程树）",
        )


def _remove_tree_quietly(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
