"""base.py 双信号退出监视器测试（T006）：假行流脚本直跑，全部 stdlib unittest。"""
import asyncio
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from flowrun.adapters.base import (  # noqa: E402
    DispatchResult,
    SignalUpdate,
    StreamMonitoringAdapter,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
FAKE_LINE_AGENT_SCRIPT = FIXTURES_DIR / "fake_line_agent.py"
PACKET_TEXT = "实现登录 API：JWT 签发与校验。"


class DoneKeywordAdapter(StreamMonitoringAdapter):
    """测试用监视器：DONE 行 = 语义完成；META 行 = 结构化字段提取。"""

    name = "done-keyword-agent"

    def parse_line(self, line: str) -> SignalUpdate:
        stripped_line = line.strip()
        if stripped_line == "DONE":
            return SignalUpdate(semantic_done=True)
        if stripped_line.startswith("META "):
            return SignalUpdate(structured_fields={"meta_line": stripped_line})
        return SignalUpdate()


def run_dispatch(adapter: StreamMonitoringAdapter, mode: str,
                 timeout_seconds: float | None) -> DispatchResult:
    with dispatch_result_scoped(adapter, mode, timeout_seconds) as result:
        return result


@contextmanager
def dispatch_result_scoped(adapter: StreamMonitoringAdapter, mode: str,
                           timeout_seconds: float | None):
    """在 work_dir 存活期内产出 DispatchResult（审计文件断言须在此作用域内做）。"""
    with tempfile.TemporaryDirectory(prefix="flowrun-base-test-") as work_dir_name:
        work_dir = Path(work_dir_name)
        packet_path = work_dir / "packet.md"
        packet_path.write_text(PACKET_TEXT, encoding="utf-8")
        agent_command = [
            sys.executable, str(FAKE_LINE_AGENT_SCRIPT), "--mode", mode,
        ]
        yield asyncio.run(
            adapter.dispatch(packet_path, agent_command, work_dir, timeout_seconds)
        )


class NaturalExitMonitorTest(unittest.TestCase):
    def test_natural_exit_after_done_records_success(self):
        result = run_dispatch(DoneKeywordAdapter(), mode="normal", timeout_seconds=30)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.timed_out)
        self.assertTrue(result.semantic_done)

    def test_natural_exit_collects_structured_fields_and_audit(self):
        # 审计断言必须在 work_dir 仍存活时做：用上下文管理器圈住作用域
        with dispatch_result_scoped(DoneKeywordAdapter(), mode="normal",
                                    timeout_seconds=30) as result:
            self.assertEqual(result.structured_result.get("meta_line"), "META answer=42")
            stdout_audit = result.stdout_path.read_text(encoding="utf-8")
            self.assertIn("DONE", stdout_audit)
            self.assertIn("META answer=42", stdout_audit)
            # packet 投递核对：假脚本把收到的 packet 回显到 stderr 审计文件
            stderr_audit = result.stderr_path.read_text(encoding="utf-8")
            self.assertIn(PACKET_TEXT, stderr_audit)

    def test_exit_failure_without_done_is_not_semantic_done(self):
        result = run_dispatch(DoneKeywordAdapter(), mode="exit-failure", timeout_seconds=30)
        self.assertEqual(result.exit_code, 2)
        self.assertFalse(result.timed_out)
        self.assertFalse(result.semantic_done)


class SemanticGraceMonitorTest(unittest.TestCase):
    def test_hanging_process_after_done_is_killed_within_grace(self):
        # 语义完成后进程不退（sleep 60s）：监视器在宽限期（注入为 0.2s）后 kill
        adapter = DoneKeywordAdapter(semantic_grace_seconds=0.2)
        result = run_dispatch(adapter, mode="hang-after-done", timeout_seconds=30)
        self.assertTrue(result.semantic_done)
        self.assertFalse(result.timed_out)
        self.assertNotEqual(result.exit_code, 0)
        self.assertLess(result.duration_seconds, 30)  # 没有傻等满 60s

    def test_default_grace_is_thirty_seconds(self):
        from flowrun.adapters.base import DEFAULT_SEMANTIC_GRACE_SECONDS
        self.assertEqual(
            DoneKeywordAdapter().semantic_grace_seconds, DEFAULT_SEMANTIC_GRACE_SECONDS
        )


class OverallTimeoutMonitorTest(unittest.TestCase):
    def test_silent_hang_is_killed_on_timeout(self):
        result = run_dispatch(
            DoneKeywordAdapter(), mode="silent-hang", timeout_seconds=0.5
        )
        self.assertTrue(result.timed_out)
        self.assertFalse(result.semantic_done)
        self.assertNotEqual(result.exit_code, 0)
        self.assertLess(result.duration_seconds, 30)


class DispatchResultShapeTest(unittest.TestCase):
    def test_dispatch_result_default_factory_isolation(self):
        first_result = DispatchResult(
            exit_code=0, timed_out=False, semantic_done=True,
            stdout_path=Path("a.log"), duration_seconds=1.0,
        )
        first_result.structured_result["session_id"] = "s1"
        second_result = DispatchResult(
            exit_code=0, timed_out=False, semantic_done=True,
            stdout_path=Path("b.log"), duration_seconds=1.0,
        )
        self.assertEqual(second_result.structured_result, {})


class NonWindowsLaunchPassthroughTest(unittest.TestCase):
    """C2 边界：非 win32 分支必须原样透传 argv（行为与引入启动计划前逐字节一致）。

    回归目标：启动计划重构若把"解析不到命令头就抛错"写成无条件分支，
    POSIX 上原本交给 CreateProcess/execvp 的原样 argv 就会变成异常。
    """

    def _plan_with_platform(self, platform_name: str, command: list[str]):
        from flowrun.adapters.base import plan_windows_command_launch

        with mock.patch("flowrun.adapters.base.sys.platform", platform_name):
            return plan_windows_command_launch(command)

    def test_posix_unresolvable_head_is_passed_through_without_rewrite(self):
        command = ["flowrun-definitely-missing-cli", "run", "arg with space"]
        launch_plan = self._plan_with_platform("linux", command)
        self.assertIsNone(launch_plan.shell_command_line)
        self.assertEqual(list(launch_plan.exec_arguments), command)

    def test_posix_existing_executable_is_passed_through_unchanged(self):
        command = [sys.executable, "-c", "print('hi')"]
        launch_plan = self._plan_with_platform("darwin", command)
        self.assertIsNone(launch_plan.shell_command_line)
        self.assertEqual(list(launch_plan.exec_arguments), command)


if __name__ == "__main__":
    unittest.main()
