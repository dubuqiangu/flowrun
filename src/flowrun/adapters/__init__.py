"""适配层（域C）：文件契约面 + 双信号退出监视器（interfaces.md §4）。

导入本包即触发内置适配器的声明式注册（claude-code / opencode）。
"""
from flowrun.adapters import claude_code, opencode_cli
from flowrun.adapters.base import (
    AgentAdapter,
    DispatchResult,
    SignalUpdate,
    StreamMonitoringAdapter,
)
from flowrun.adapters.registry import (
    INJECTION_MODE_CONTENT,
    INJECTION_MODE_PATH,
    AgentCommandSpec,
    expand_command_template,
    probe_available,
    register_adapter,
    resolve_adapter_class,
)

__all__ = [
    "claude_code",
    "opencode_cli",
    "AgentAdapter",
    "DispatchResult",
    "SignalUpdate",
    "StreamMonitoringAdapter",
    "AgentCommandSpec",
    "INJECTION_MODE_CONTENT",
    "INJECTION_MODE_PATH",
    "expand_command_template",
    "probe_available",
    "register_adapter",
    "resolve_adapter_class",
]
