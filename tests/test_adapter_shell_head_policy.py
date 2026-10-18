"""缺陷 3：已知 shell 命令头 + content 注入模式必须在编译期拒绝。

命令头**本身**是 shell（``cmd`` / ``powershell`` / ``pwsh`` / ``sh`` / ``bash``）
时，content 模式会把 packet 正文作为位置参数送进 argv，被该 shell 重新分词
（重定向 / 管道 / 命令分隔符）。默认多行 packet 不可触发，但这仍是理论绕过面。
**不**自动改 path 模式——把 packet 路径喂给一个 shell 会试图执行它，比拒绝更危险。

校验入口 ``validate_agent_injection_policy`` 面向编译期（命令模板定型时）：
接收 ``FlowIR.agent_registry`` 形状的映射，命中即抛
``ShellCommandHeadContentInjectionError``。
"""
import sys
import unittest
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from flowrun.adapters.registry import (  # noqa: E402
    INJECTION_MODE_CONTENT,
    INJECTION_MODE_PATH,
    AgentCommandSpec,
    ShellCommandHeadContentInjectionError,
    is_known_shell_command_head,
    require_safe_injection_for_command_head,
    validate_agent_injection_policy,
)


class ShellHeadDetectionTest(unittest.TestCase):
    """命令头识别：大小写不敏感、可带目录；非 shell 头不误判。"""

    def test_documented_shell_heads_are_recognized(self):
        for shell_head in ("cmd", "cmd.exe", "powershell", "powershell.exe",
                           "pwsh", "pwsh.exe", "sh", "bash"):
            self.assertTrue(is_known_shell_command_head(shell_head), shell_head)

    def test_head_detection_ignores_case_and_directory(self):
        self.assertTrue(is_known_shell_command_head("CMD"))
        self.assertTrue(is_known_shell_command_head(r"C:\Windows\System32\cmd.exe"))
        self.assertTrue(is_known_shell_command_head("/usr/bin/bash"))

    def test_non_shell_heads_are_not_recognized(self):
        for other_head in ("claude", "opencode", "python", "flowrun-unknown"):
            self.assertFalse(is_known_shell_command_head(other_head), other_head)


class CommandHeadInjectionPolicyTest(unittest.TestCase):
    """逐命令头判定：shell + content 拒绝；shell + path 与普通头 + content 放行。"""

    def test_shell_head_with_content_mode_is_rejected(self):
        with self.assertRaises(ShellCommandHeadContentInjectionError) as raised_context:
            require_safe_injection_for_command_head("cmd", INJECTION_MODE_CONTENT)
        error_message = str(raised_context.exception)
        self.assertIn("shell", error_message)
        self.assertIn("__FLOW_PACKET_PATH__", error_message)

    def test_shell_head_with_path_mode_is_allowed(self):
        require_safe_injection_for_command_head("cmd", INJECTION_MODE_PATH)

    def test_non_shell_head_with_content_mode_is_allowed(self):
        require_safe_injection_for_command_head("claude", INJECTION_MODE_CONTENT)


class AgentRegistryInjectionPolicyTest(unittest.TestCase):
    """编译期入口：按 agent_registry（FlowIR 冻结形状）逐个 agent 校验。"""

    def test_registry_with_shell_content_agent_is_rejected_at_compile_time(self):
        agent_registry = {
            "dangerous": AgentCommandSpec(
                command=["cmd", "/c", "echo", "__FLOW_PACKET__"], resume_mode=None),
        }
        with self.assertRaises(ShellCommandHeadContentInjectionError) as raised_context:
            validate_agent_injection_policy(agent_registry)
        self.assertIn("dangerous", str(raised_context.exception))

    def test_registry_with_plain_agent_is_allowed(self):
        agent_registry = {
            "python-agent": AgentCommandSpec(
                command=[sys.executable, "-m", "flowrun_fake_agent"], resume_mode=None),
        }
        validate_agent_injection_policy(agent_registry)

    def test_empty_registry_is_allowed(self):
        validate_agent_injection_policy({})


if __name__ == "__main__":
    unittest.main()
