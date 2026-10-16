"""Windows 垫片解析测试：npm 风格 .cmd/.bat/.ps1 垫片经 cmd /c 或 powershell 启动。

真机取证：Windows 上 `opencode` 解析为 opencode.ps1 / opencode.cmd 垫片，
shutil.which 找得到（probe 通过）但 CreateProcess 无法直接 exec（spawn 报
WinError 2），必须在适配层做垫片解析。非 Windows 平台本文件整体跳过。
"""
import asyncio
import os
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
    StreamMonitoringAdapter,
    resolve_windows_command_shim,
)

IS_WINDOWS = sys.platform == "win32"


class ResolveWindowsCommandShimTest(unittest.TestCase):
    """纯函数单测：垫片识别与命令重写规则。"""

    def setUp(self):
        if not IS_WINDOWS:
            self.skipTest("Windows 专属行为")
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="flowrun_shim_"))
        self.addCleanup(lambda: _remove_tree_quietly(self.tmp_dir))

    def _write_shim(self, filename: str) -> Path:
        shim_path = self.tmp_dir / filename
        shim_path.write_text("@echo off\r\n", encoding="ascii")
        return shim_path

    def test_cmd_shim_rewritten_via_cmd_c(self):
        shim_path = self._write_shim("fake_agent.cmd")
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp_dir)}):
            rewritten = resolve_windows_command_shim(
                ["fake_agent", "-p", "--packet", "x.md"]
            )
        self.assertEqual(rewritten[0], "cmd")
        self.assertEqual(rewritten[1], "/c")
        # shutil.which 在 Windows 可能返回大写扩展名（fake_agent.CMD），路径比较不区分大小写。
        self.assertEqual(str(Path(rewritten[2])).casefold(), str(shim_path).casefold())
        self.assertEqual(rewritten[3:], ["-p", "--packet", "x.md"])

    def test_bat_shim_rewritten_via_cmd_c(self):
        self._write_shim("fake_agent.bat")
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp_dir)}):
            rewritten = resolve_windows_command_shim(["fake_agent"])
        self.assertEqual(rewritten[:2], ["cmd", "/c"])

    def test_ps1_shim_rewritten_via_powershell(self):
        ps1_path = self.tmp_dir / "fake_agent.ps1"
        ps1_path.write_text("Write-Output ok\n", encoding="ascii")
        # 默认 PATHEXT 不含 .PS1，须显式补上（真机 opencode.ps1 场景：npm 垫片通常同时有 .cmd）。
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp_dir),
                                          "PATHEXT": ".COM;.EXE;.CMD;.PS1"}):
            rewritten = resolve_windows_command_shim(["fake_agent"])
        self.assertEqual(rewritten[0], "powershell")
        self.assertIn("-File", rewritten)
        self.assertEqual(str(Path(rewritten[-1])).casefold(), str(ps1_path).casefold())

    def test_plain_command_untouched_when_not_resolvable(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp_dir)}):
            rewritten = resolve_windows_command_shim(
                ["no_such_cli_anywhere", "run"]
            )
        self.assertEqual(rewritten, ["no_such_cli_anywhere", "run"])

    def test_empty_command_untouched(self):
        self.assertEqual(resolve_windows_command_shim([]), [])


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
    for entry in directory.iterdir():
        if entry.is_file():
            entry.unlink()
    directory.rmdir()


if __name__ == "__main__":
    unittest.main()
