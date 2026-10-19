"""Windows 启动计划：命令模板 → 直接 exec 的 argv 或经 cmd.exe 的整条命令行。

从 ``flowrun.adapters.base`` 拆出（纯结构调整，行为零变化）：启动方式解析与
审计落盘/流式监视是两件独立的职责，分开后改启动细节不必再碰 dispatch 主流程。

``base`` 会把这里的公开名字原样重新导出，历史导入路径
``from flowrun.adapters.base import plan_windows_command_launch, WindowsLaunchPlan``
保持可用。
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from flowrun.adapters.cmd_line_safety import render_cmd_exe_arguments
from flowrun.adapters.registry import (
    WINDOWS_BATCH_SHIM_SUFFIXES,
    WINDOWS_COMMAND_SUFFIX_CANDIDATES,
    WINDOWS_POWERSHELL_SHIM_SUFFIXES,
    resolve_command_path,
)

__all__ = [
    "TASKKILL_TIMEOUT_SECONDS",
    "CommandHeadUnresolvableError",
    "WindowsLaunchPlan",
    "plan_windows_command_launch",
    "taskkill_process_tree",
]

# taskkill /T /F 的自限时：绝不让收尸动作本身挂住 dispatch。
TASKKILL_TIMEOUT_SECONDS = 10.0


class CommandHeadUnresolvableError(FileNotFoundError):
    """适配层错误：命令头在本机不可解析（PATH 与扩展名候选集都没命中）。

    继承 ``FileNotFoundError`` 是刻意的：win32 上 CreateProcess 找不到命令时抛的
    就是 ``FileNotFoundError: [WinError 2] 系统找不到指定的文件``，调用方现有的
    OSError 兜底路径不用改，而消息本身变成了能看懂的一句。
    """


@dataclass(frozen=True)
class WindowsLaunchPlan:
    """win32 启动方式：直接 exec 的 argv，或交经 cmd.exe 的整条命令行（二选一）。

    批处理垫片（.cmd/.bat）只能走后者。``cmd /c <shim> <args>`` 的 argv 形态在
    "垫片路径含空格 + 参数含空格"时会产生 4 个引号，触发 cmd.exe 的引号剥离
    启发式：垫片根本没跑，rc=1、stdout 为空（实测）。整串形态下 CPython 的 Popen
    生成 ``cmd.exe /c "<render_cmd_exe_arguments 结果>"``，cmd 剥掉外层引号后正好是
    ``"<shim>" "<arg>"...``（每个 token 都已带引号）——这正是 cmd 认可的形态
    （实测三种空格组合全部 rc=0）。
    """

    exec_arguments: tuple[str, ...] | None = None
    shell_command_line: str | None = None

    def __post_init__(self) -> None:
        if (self.exec_arguments is None) == (self.shell_command_line is None):
            raise ValueError(
                "WindowsLaunchPlan 必须且只能给一个："
                "exec_arguments 或 shell_command_line"
            )


def plan_windows_command_launch(command: list[str]) -> WindowsLaunchPlan:
    """把（已展开占位符的）命令模板解析成启动方式。

    npm 等包管理器安装的 CLI 是 .cmd/.bat/.ps1 垫片，CreateProcess 无法直接 exec
    （spawn 报 WinError 2，而 which 找得到它 → probe 通过、dispatch 崩溃）：

    - .cmd/.bat → 经 cmd.exe 的整条命令行（见 WindowsLaunchPlan）；
    - .ps1 → ``powershell -NoProfile -ExecutionPolicy Bypass -File <shim>``；
    - 其余（.exe、显式路径、非 win32）→ 原样 exec。

    win32 上命令头解析不到时抛 ``CommandHeadUnresolvableError``，不让 WinError 2
    穿透（命令原样交给 CreateProcess 只会得到同义但没人看得懂的一句）。
    """
    if not command:
        return WindowsLaunchPlan(exec_arguments=())
    resolved_path = resolve_command_path(command[0])
    if resolved_path is None:
        if sys.platform != "win32":
            return WindowsLaunchPlan(exec_arguments=tuple(command))
        raise CommandHeadUnresolvableError(
            f"命令头 '{command[0]}' 在本机不可解析：不在 PATH 上，也没有 "
            f"{'/'.join(WINDOWS_COMMAND_SUFFIX_CANDIDATES)} 任一扩展名的可执行文件。"
            " 请修正 frontmatter 里的 cmd:（或先安装这个 CLI）。"
        )
    resolved_suffix = Path(resolved_path).suffix.lower()
    if resolved_suffix in WINDOWS_BATCH_SHIM_SUFFIXES:
        # 批处理垫片经 cmd.exe 执行：每个参数无条件加双引号（cmd 元字符失活），
        # 含 %/"/\r/\n 的参数失败关闭（cmd 会展开 % 并重分词）。
        return WindowsLaunchPlan(
            shell_command_line=render_cmd_exe_arguments([resolved_path, *command[1:]])
        )
    if resolved_suffix in WINDOWS_POWERSHELL_SHIM_SUFFIXES:
        return WindowsLaunchPlan(exec_arguments=(
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", resolved_path, *command[1:],
        ))
    return WindowsLaunchPlan(exec_arguments=tuple(command))


def taskkill_process_tree(process_id: int) -> bool:
    """``taskkill /T /F /PID``：连坐终止整棵进程树。成功返回 True。

    用 stdlib subprocess 调系统 taskkill，不引入额外依赖。taskkill 缺失/超时/
    非零退出都返回 False，交给调用方回退 ``process.kill()``。
    """
    try:
        completed_process = subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process_id)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=TASKKILL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed_process.returncode == 0