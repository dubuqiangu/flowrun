"""两个真实适配器冒烟测试（T007/T008）：假可执行脚本跑通投递 + 双信号闭环。"""
import asyncio
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

import flowrun.adapters  # noqa: F401,E402  # 触发内置声明式注册
from flowrun.adapters.claude_code import ClaudeCodeAdapter  # noqa: E402
from flowrun.adapters.opencode_cli import OpenCodeCliAdapter  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
FAKE_CLAUDE_SCRIPT = FIXTURES_DIR / "fake_claude.py"
FAKE_OPENCODE_SCRIPT = FIXTURES_DIR / "fake_opencode.py"
PACKET_TEXT = "实现登录 API：JWT 签发与校验、bcrypt 存储。"


def run_dispatch(adapter, fake_script: Path, mode: str,
                 timeout_seconds: float | None):
    with dispatch_result_scoped(adapter, fake_script, mode, timeout_seconds) as result:
        return result


@contextmanager
def dispatch_result_scoped(adapter, fake_script: Path, mode: str,
                           timeout_seconds: float | None):
    """在 work_dir 存活期内产出 DispatchResult（审计文件断言须在此作用域内做）。"""
    with tempfile.TemporaryDirectory(prefix="flowrun-smoke-test-") as work_dir_name:
        work_dir = Path(work_dir_name)
        packet_path = work_dir / "packet.md"
        packet_path.write_text(PACKET_TEXT, encoding="utf-8")
        agent_command = [
            sys.executable, str(fake_script), "--mode", mode,
        ]
        yield asyncio.run(
            adapter.dispatch(packet_path, agent_command, work_dir, timeout_seconds)
        )


class ClaudeCodeAdapterSmokeTest(unittest.TestCase):
    def test_normal_dispatch_delivers_packet_and_captures_result(self):
        with dispatch_result_scoped(
            ClaudeCodeAdapter(), FAKE_CLAUDE_SCRIPT, mode="normal", timeout_seconds=30
        ) as result:
            self.assertEqual(result.exit_code, 0)
            self.assertTrue(result.semantic_done)
            self.assertFalse(result.timed_out)
            # 语义结果提取：session_id 与成本字段进了 structured_result
            self.assertEqual(
                result.structured_result.get("session_id"), "fake-claude-session-0001"
            )
            self.assertEqual(result.structured_result.get("total_cost_usd"), 0.42)
            self.assertEqual(
                result.structured_result.get("usage"),
                {"input_tokens": 100, "output_tokens": 200},
            )
            # 原始 stream-json 全量落盘审计
            stdout_audit = result.stdout_path.read_text(encoding="utf-8")
            self.assertIn('"type": "result"', stdout_audit)
            # packet 确实作为最后位置参数投递（假脚本回显在 stderr 审计文件）
            stderr_audit = result.stderr_path.read_text(encoding="utf-8")
            self.assertIn(PACKET_TEXT, stderr_audit)

    def test_hanging_after_result_is_killed_within_grace(self):
        adapter = ClaudeCodeAdapter(semantic_grace_seconds=0.2)
        result = run_dispatch(
            adapter, FAKE_CLAUDE_SCRIPT, mode="hang-after-result", timeout_seconds=30
        )
        self.assertTrue(result.semantic_done)
        self.assertFalse(result.timed_out)
        self.assertNotEqual(result.exit_code, 0)
        self.assertLess(result.duration_seconds, 30)

    def test_overall_timeout_kills_silent_hang(self):
        result = run_dispatch(
            ClaudeCodeAdapter(), FAKE_CLAUDE_SCRIPT, mode="silent-hang",
            timeout_seconds=0.5,
        )
        self.assertTrue(result.timed_out)
        self.assertFalse(result.semantic_done)
        self.assertNotEqual(result.exit_code, 0)

    def test_fail_without_result_keeps_semantic_done_false(self):
        result = run_dispatch(
            ClaudeCodeAdapter(), FAKE_CLAUDE_SCRIPT, mode="fail-no-result",
            timeout_seconds=30,
        )
        self.assertEqual(result.exit_code, 3)
        self.assertFalse(result.semantic_done)
        self.assertFalse(result.timed_out)


class OpenCodeCliAdapterSmokeTest(unittest.TestCase):
    def test_normal_dispatch_detects_final_assistant_message(self):
        with dispatch_result_scoped(
            OpenCodeCliAdapter(), FAKE_OPENCODE_SCRIPT, mode="normal", timeout_seconds=30
        ) as result:
            self.assertEqual(result.exit_code, 0)
            self.assertTrue(result.semantic_done)
            self.assertFalse(result.timed_out)
            self.assertEqual(
                result.structured_result.get("id"), "msg_fake_opencode_0001"
            )
            self.assertEqual(result.structured_result.get("cost"), 0.05)
            stdout_audit = result.stdout_path.read_text(encoding="utf-8")
            self.assertIn("msg_fake_opencode_0001", stdout_audit)
            stderr_audit = result.stderr_path.read_text(encoding="utf-8")
            self.assertIn(PACKET_TEXT, stderr_audit)

    def test_hanging_after_message_is_killed_within_grace(self):
        adapter = OpenCodeCliAdapter(semantic_grace_seconds=0.2)
        result = run_dispatch(
            adapter, FAKE_OPENCODE_SCRIPT, mode="hang-after-message", timeout_seconds=30
        )
        self.assertTrue(result.semantic_done)
        self.assertFalse(result.timed_out)
        self.assertNotEqual(result.exit_code, 0)
        self.assertLess(result.duration_seconds, 30)

    def test_overall_timeout_kills_silent_hang(self):
        result = run_dispatch(
            OpenCodeCliAdapter(), FAKE_OPENCODE_SCRIPT, mode="silent-hang",
            timeout_seconds=0.5,
        )
        self.assertTrue(result.timed_out)
        self.assertFalse(result.semantic_done)
        self.assertNotEqual(result.exit_code, 0)

    def test_fail_without_completion_shape_keeps_semantic_done_false(self):
        result = run_dispatch(
            OpenCodeCliAdapter(), FAKE_OPENCODE_SCRIPT, mode="fail-no-message",
            timeout_seconds=30,
        )
        self.assertEqual(result.exit_code, 3)
        self.assertFalse(result.semantic_done)
        self.assertFalse(result.timed_out)


if __name__ == "__main__":
    unittest.main()
