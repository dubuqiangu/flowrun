"""registry.py 测试（T006）：which 探测 + 占位符展开 + 声明式注册 + AgentCommandSpec。"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from flowrun.adapters.base import StreamMonitoringAdapter  # noqa: E402
from flowrun.adapters.registry import (  # noqa: E402
    PACKET_CONTENT_PLACEHOLDER,
    PACKET_PATH_PLACEHOLDER,
    AgentCommandSpec,
    create_adapter_for_command,
    expand_command_template,
    probe_available,
    register_adapter,
    resolve_adapter_class,
    resolve_command_path,
)

PACKET_TEXT = "任务包正文：实现登录 API。"


def make_packet_file() -> Path:
    temp_dir = tempfile.mkdtemp(prefix="flowrun-registry-test-")
    packet_path = Path(temp_dir) / "packet.md"
    packet_path.write_text(PACKET_TEXT, encoding="utf-8")
    return packet_path


class ProbeAvailableTest(unittest.TestCase):
    def test_existing_executable_probes_true(self):
        # sys.executable 是本机必然存在的可执行路径
        agent_registry = {"python-agent": AgentCommandSpec(
            command=[sys.executable], resume_mode=None)}
        self.assertEqual(probe_available(agent_registry), {"python-agent": True})

    def test_missing_executable_probes_false(self):
        agent_registry = {"ghost-agent": AgentCommandSpec(
            command=["flowrun-definitely-missing-agent-xyz"], resume_mode=None)}
        self.assertEqual(probe_available(agent_registry), {"ghost-agent": False})

    def test_empty_command_probes_false(self):
        agent_registry = {"empty-agent": AgentCommandSpec(command=[], resume_mode=None)}
        self.assertEqual(probe_available(agent_registry), {"empty-agent": False})


class ExpandCommandTemplateTest(unittest.TestCase):
    def test_default_appends_packet_content_as_last_argument(self):
        packet_path = make_packet_file()
        expanded = expand_command_template(["agent", "run"], packet_path)
        self.assertEqual(expanded, ["agent", "run", PACKET_TEXT])

    def test_content_placeholder_replaced_with_packet_text(self):
        packet_path = make_packet_file()
        expanded = expand_command_template(
            ["agent", "--prompt", PACKET_CONTENT_PLACEHOLDER], packet_path
        )
        self.assertEqual(expanded, ["agent", "--prompt", PACKET_TEXT])

    def test_content_placeholder_as_whole_argument(self):
        packet_path = make_packet_file()
        expanded = expand_command_template(
            ["agent", PACKET_CONTENT_PLACEHOLDER], packet_path
        )
        self.assertEqual(expanded, ["agent", PACKET_TEXT])

    def test_path_placeholder_replaced_with_absolute_path(self):
        packet_path = make_packet_file()
        expanded = expand_command_template(
            ["agent", "--packet", PACKET_PATH_PLACEHOLDER], packet_path
        )
        self.assertEqual(expanded, ["agent", "--packet", str(packet_path.resolve())])

    def test_path_placeholder_yields_existing_absolute_path(self):
        packet_path = make_packet_file()
        expanded = expand_command_template(
            ["agent", PACKET_PATH_PLACEHOLDER], packet_path
        )
        injected_path = Path(expanded[1])
        self.assertTrue(injected_path.is_absolute())
        self.assertTrue(injected_path.exists())


class ExpandCommandTemplateInjectionModeTest(unittest.TestCase):
    """FIX-1：injection_mode="path" 时只投递 packet 路径，绝不投递正文。"""

    def test_path_mode_appends_packet_path_when_no_placeholder(self):
        packet_path = make_packet_file()
        expanded = expand_command_template(
            ["agent", "run"], packet_path, injection_mode="path"
        )
        self.assertEqual(expanded, ["agent", "run", str(packet_path.resolve())])
        self.assertNotIn(PACKET_TEXT, " ".join(expanded))

    def test_path_mode_replaces_content_placeholder_with_path(self):
        packet_path = make_packet_file()
        expanded = expand_command_template(
            ["agent", "--packet", PACKET_CONTENT_PLACEHOLDER],
            packet_path,
            injection_mode="path",
        )
        self.assertEqual(expanded, ["agent", "--packet", str(packet_path.resolve())])

    def test_path_mode_replaces_path_placeholder_with_path(self):
        packet_path = make_packet_file()
        expanded = expand_command_template(
            ["agent", PACKET_PATH_PLACEHOLDER],
            packet_path,
            injection_mode="path",
        )
        self.assertEqual(expanded, ["agent", str(packet_path.resolve())])

    def test_content_mode_is_the_default(self):
        packet_path = make_packet_file()
        self.assertEqual(
            expand_command_template(["agent", "run"], packet_path),
            expand_command_template(["agent", "run"], packet_path,
                                    injection_mode="content"),
        )

    def test_unknown_injection_mode_is_rejected(self):
        packet_path = make_packet_file()
        with self.assertRaises(ValueError):
            expand_command_template(["agent"], packet_path, injection_mode="bogus")


class AdapterLookupCaseInsensitivityTest(unittest.TestCase):
    """FIX-9：命令头匹配不区分大小写（Windows which 能探到 'Claude'）。"""

    def test_mixed_case_command_head_resolves_to_registered_adapter(self):
        canonical_adapter = resolve_adapter_class("claude")
        self.assertIsNotNone(canonical_adapter)
        self.assertIs(resolve_adapter_class("Claude"), canonical_adapter)
        self.assertIs(resolve_adapter_class("CLAUDE"), canonical_adapter)

    def test_mixed_case_command_head_creates_adapter_instance(self):
        import flowrun.adapters  # noqa: F401  # 触发内置声明式注册
        adapter = create_adapter_for_command(["Claude", "-p"])
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.name, "claude-code")


class AgentCommandSpecContractTest(unittest.TestCase):
    def test_spec_field_shape_matches_frozen_contract(self):
        spec = AgentCommandSpec(
            command=["claude", "-p", "--output-format", "stream-json"],
            resume_mode="session",
        )
        self.assertEqual(spec.command[0], "claude")
        self.assertEqual(spec.resume_mode, "session")


class DeclarativeRegistrationTest(unittest.TestCase):
    def test_builtin_adapters_registered_by_import(self):
        self.assertIsNotNone(resolve_adapter_class("claude"))
        self.assertIsNotNone(resolve_adapter_class("opencode"))

    def test_create_adapter_for_command_returns_named_instance(self):
        import flowrun.adapters  # noqa: F401  # 触发内置声明式注册
        adapter = create_adapter_for_command(["opencode", "run", "--format", "json"])
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.name, "opencode")

    def test_unknown_command_head_resolves_to_none(self):
        self.assertIsNone(resolve_adapter_class("flowrun-unknown-cli"))
        self.assertIsNone(create_adapter_for_command(["flowrun-unknown-cli", "x"]))

    def test_register_adapter_maps_command_heads(self):
        @register_adapter
        class RegisteredProbeAdapter(StreamMonitoringAdapter):
            name = "registered-probe-agent"
            command_heads = ("flowrun-probe-cli",)

        self.assertIs(resolve_adapter_class("flowrun-probe-cli"), RegisteredProbeAdapter)
        probe_adapter = create_adapter_for_command(["flowrun-probe-cli"])
        self.assertIsInstance(probe_adapter, RegisteredProbeAdapter)

    def test_empty_command_returns_none(self):
        self.assertIsNone(create_adapter_for_command([]))


class WindowsPathEntryResolutionTest(unittest.TestCase):
    """C3：探测与启动必须用同一条解析路径（.ps1-only 垫片不再报"不可用"）。

    回归目标：PATHEXT 默认不含 ``.PS1``，``shutil.which("x")`` 因此漏掉
    ``x.ps1``；旧实现在这里报 probe False、dispatch WinError 2。现在
    ``resolve_command_path`` 与 ``probe_available`` 共享同一解析，结论一致。
    """

    @unittest.skipUnless(sys.platform == "win32", "win32 专属的命令头解析行为")
    def test_ps1_only_head_is_resolvable_and_probes_available_without_pathext_entry(self):
        with tempfile.TemporaryDirectory(prefix="flowrun-c3-ps1-") as temp_dir_name:
            ps1_path = Path(temp_dir_name) / "flowrun_c3_ps_only_agent.ps1"
            ps1_path.write_text("Write-Output ok\n", encoding="ascii")
            saved_path = os.environ.get("PATH")
            saved_pathext = os.environ.get("PATHEXT")
            # PATHEXT 明确不含 .PS1：which 看不到，只有字面名兜底探测能找到。
            os.environ["PATH"] = temp_dir_name
            os.environ["PATHEXT"] = ".COM;.EXE;.CMD"
            try:
                self.assertIsNone(
                    shutil.which("flowrun_c3_ps_only_agent"),
                    "前置条件：PATHEXT 不含 .PS1 时 which 必须漏掉该命令",
                )
                self.assertEqual(
                    resolve_command_path("flowrun_c3_ps_only_agent"), str(ps1_path)
                )
                agent_registry = {"ps-agent": AgentCommandSpec(
                    command=["flowrun_c3_ps_only_agent"], resume_mode=None)}
                self.assertEqual(probe_available(agent_registry), {"ps-agent": True})
            finally:
                if saved_path is None:
                    os.environ.pop("PATH", None)
                else:
                    os.environ["PATH"] = saved_path
                if saved_pathext is None:
                    os.environ.pop("PATHEXT", None)
                else:
                    os.environ["PATHEXT"] = saved_pathext


if __name__ == "__main__":
    unittest.main()
