"""声明式适配器注册表 + which 探测 + __FLOW_*__ 占位符机制（interfaces.md §4）。

spec-kit 模式（design.md §2.2 现场查证结论照抄）：一份命令模板 + 占位符
+ 每 CLI 一个声明式薄适配器，拒绝三方言脚本镜像。

占位符机制：
- __FLOW_PACKET__      → 替换为 packet 文件内容（整参数或嵌在参数内均可）；
- __FLOW_PACKET_PATH__ → 替换为 packet 文件绝对路径；
- 默认行为（模板不含任何占位符）：packet 文件内容追加为最后一个位置参数，
  即 `claude -p "$(cat packet.md)"` 语义。

注入模式（injection_mode，安全约束）：
- ``"content"``（默认）投递 packet 正文；
- ``"path"`` 只投递 packet 绝对路径，**一个正文片段都不进 argv**。
  Windows 的 .cmd/.bat 垫片必须用这个模式：``cmd /c shim <args>`` 的命令行
  会被 cmd.exe 重新分词，正文里的 ``"``、``&``、``|`` 会被当成命令分隔符，
  等于把 packet 内容当代码执行（任意命令注入）。垫片下真 agent 需要读正文时
  自己读这个路径。两种占位符在 path 模式下都替换为路径。
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅供类型标注，运行期不导入（base.py 依赖本模块，避免环）
    from flowrun.adapters.base import AgentAdapter

try:  # 域A（ir/schema.py，T001）已落地并核对一致（2026-10-15），此导入生效
    from flowrun.ir.schema import AgentCommandSpec
except ImportError:
    # TODO-CONTRACT: 防御性 fallback（字段名照 interfaces.md §4 冻结形状：
    # command: list[str], resume_mode: str | None）。仅当 ir/schema.py 不可
    # 导入时兜底；orchestrator 可在确认域A稳定后删除本段。
    @dataclass
    class AgentCommandSpec:  # type: ignore[no-redef]
        command: list[str]
        resume_mode: str | None


PACKET_CONTENT_PLACEHOLDER = "__FLOW_PACKET__"
PACKET_PATH_PLACEHOLDER = "__FLOW_PACKET_PATH__"

INJECTION_MODE_CONTENT = "content"
INJECTION_MODE_PATH = "path"
_INJECTION_MODES = (INJECTION_MODE_CONTENT, INJECTION_MODE_PATH)

# win32 命令头解析与启动方式的扩展名知识（base.py 复用，避免两处各写一份）：
# - 候选集用于 which 未命中时的有界兜底探测；
# - 垫片（shim）是包管理器装的脚本包装器，CreateProcess 无法直接 exec，
#   必须换一种启动方式（批处理经 cmd.exe，PowerShell 经 powershell -File）。
WINDOWS_COMMAND_SUFFIX_CANDIDATES = (".exe", ".cmd", ".bat", ".ps1")
WINDOWS_BATCH_SHIM_SUFFIXES = (".cmd", ".bat")
WINDOWS_POWERSHELL_SHIM_SUFFIXES = (".ps1",)
WINDOWS_SHIM_SUFFIXES = WINDOWS_BATCH_SHIM_SUFFIXES + WINDOWS_POWERSHELL_SHIM_SUFFIXES

# 命令头（command[0]）→ 适配器类。内置项由 claude_code / opencode_cli
# 模块级 @register_adapter 装饰器在导入时填充。键一律 casefold()：
# Windows which 探测不区分大小写（能找到 'Claude'），查找也必须不区分，
# 否则会静默降级到 FallbackAdapter、永久丢失适配器的语义完成信号。
_ADAPTERS_BY_COMMAND_HEAD: dict[str, type] = {}


def register_adapter(adapter_class: type) -> type:
    """声明式注册：把适配器类按其 command_heads 登记进全局表。

    适配器类需声明 ``command_heads: tuple[str, ...]``（其默认命令模板的
    可执行名），本函数作为类装饰器使用（spec-kit 薄适配器模式）。
    命令头按 casefold() 归一后作键。
    """
    for command_head in getattr(adapter_class, "command_heads", ()):
        _ADAPTERS_BY_COMMAND_HEAD[command_head.casefold()] = adapter_class
    return adapter_class


def resolve_adapter_class(command_head: str) -> type | None:
    """按命令头（如 "claude" / "opencode"）查已注册适配器类（不区分大小写）。"""
    return _ADAPTERS_BY_COMMAND_HEAD.get(command_head.casefold())


def create_adapter_for_command(agent_command: list[str]) -> AgentAdapter | None:
    """从命令模板实例化适配器；未注册的命令头返回 None（内核转人审/报错）。"""
    if not agent_command:
        return None
    adapter_class = resolve_adapter_class(agent_command[0])
    if adapter_class is None:
        return None
    return adapter_class()  # type: ignore[no-any-return]


def expand_command_template(
    agent_command: list[str],
    packet_path: Path,
    injection_mode: str = INJECTION_MODE_CONTENT,
) -> list[str]:
    """把命令模板里的占位符展开为实际投递参数（见模块 docstring）。

    packet 路径统一 resolve 为绝对路径：适配器进程以 work_dir 为 cwd，
    相对路径会指错位置。

    ``injection_mode``:
    - ``"content"``（默认）：``__FLOW_PACKET__`` → 正文；无占位符时正文追加为
      最后一个位置参数；
    - ``"path"``：正文绝不进 argv，``__FLOW_PACKET__`` 与 ``__FLOW_PACKET_PATH__``
      都替换为 packet 绝对路径；无占位符时追加路径。Windows 批处理垫片必须走
      这个模式（cmd.exe 会把正文里的 ``&`` / ``"`` 当命令分隔符重解析）。
    """
    if injection_mode not in _INJECTION_MODES:
        raise ValueError(
            f"unknown injection_mode {injection_mode!r} "
            f"(expected one of {_INJECTION_MODES})"
        )
    content_is_allowed = injection_mode == INJECTION_MODE_CONTENT
    absolute_packet_path = Path(packet_path).resolve()
    packet_content_cache: list[str] = []  # 至多读一次的惰性缓存

    def load_packet_content() -> str:
        if not packet_content_cache:
            packet_content_cache.append(
                absolute_packet_path.read_text(encoding="utf-8")
            )
        return packet_content_cache[0]

    has_content_placeholder = any(
        PACKET_CONTENT_PLACEHOLDER in command_part for command_part in agent_command
    )
    has_path_placeholder = any(
        PACKET_PATH_PLACEHOLDER in command_part for command_part in agent_command
    )

    if not (has_content_placeholder or has_path_placeholder):
        # 默认投递：正文（content 模式）或路径（path 模式）作为最后位置参数。
        injected_argument = (
            load_packet_content() if content_is_allowed else str(absolute_packet_path)
        )
        return [*agent_command, injected_argument]

    expanded_command: list[str] = []
    for command_part in agent_command:
        if PACKET_PATH_PLACEHOLDER in command_part:
            command_part = command_part.replace(
                PACKET_PATH_PLACEHOLDER, str(absolute_packet_path)
            )
        if PACKET_CONTENT_PLACEHOLDER in command_part:
            replacement = (
                load_packet_content() if content_is_allowed else str(absolute_packet_path)
            )
            command_part = command_part.replace(PACKET_CONTENT_PLACEHOLDER, replacement)
        expanded_command.append(command_part)
    return expanded_command


def resolve_command_path(command_head: str) -> str | None:
    """解析命令头 → 可执行文件路径（决定"能不能启动"的唯一入口）；解析不到返回 None。

    第一轮 ``shutil.which``（POSIX 上就是全部逻辑，行为与本函数引入前一致）；
    win32 上未命中时再按**显式扩展名候选集**与字面名探测一轮 —— 默认 PATHEXT
    不含 ``.PS1``，只有 ``.ps1`` 垫片的 CLI（npm 全局安装常见）会被 which 漏掉，
    于是 probe 报不可用、dispatch 报 WinError 2，两边都不说人话。
    """
    if not command_head:
        return None
    resolved_path = shutil.which(command_head)
    if resolved_path is not None:
        return resolved_path
    if sys.platform != "win32":
        return None
    for candidate_name in _windows_command_candidate_names(command_head):
        resolved_path = _search_path_entries_for_literal_name(candidate_name)
        if resolved_path is not None:
            return resolved_path
    return None


def _windows_command_candidate_names(command_head: str) -> list[str]:
    """候选文件名：带扩展名的头只查字面名，裸名才追加扩展名候选集。"""
    if Path(command_head).suffix:
        return [command_head]
    return [
        command_head + candidate_suffix
        for candidate_suffix in WINDOWS_COMMAND_SUFFIX_CANDIDATES
    ]


def _search_path_entries_for_literal_name(command_name: str) -> str | None:
    """按 PATH 逐条查字面文件名（不追加任何扩展名）。

    ``shutil.which`` 在目标扩展名不在 PATHEXT 里时连字面名都不查
    （实测 ``which("x.ps1")`` 在默认 PATHEXT 下返回 None），只能自己补这一轮。
    """
    if os.path.dirname(command_name):
        return command_name if os.path.isfile(command_name) else None
    for path_entry in (os.environ.get("PATH") or "").split(os.pathsep):
        candidate_path = os.path.join(path_entry, command_name) if path_entry else command_name
        if os.path.isfile(candidate_path):
            return candidate_path
    return None


def probe_available(agent_registry: dict[str, AgentCommandSpec]) -> dict[str, bool]:
    """逐 agent 探测其命令头是否在本机可解析（见 resolve_command_path）。"""
    availability: dict[str, bool] = {}
    for agent_name, agent_spec in agent_registry.items():
        availability[agent_name] = bool(
            agent_spec.command
            and resolve_command_path(agent_spec.command[0]) is not None
        )
    return availability
