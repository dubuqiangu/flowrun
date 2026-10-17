"""适配层基座：DispatchResult / AgentAdapter 协议 / 双信号退出监视器。

interfaces.md §4 冻结 API；design.md §7 风险清单要求"双信号退出监视器"
（参考 vibe-kanban 的 select! 竞速思路）：agent CLI 经常"跑完不退"或
"挂死不报"，单靠退出码无法区分，故三信号竞速：

1. 进程自然退出（拿 exit_code）；
2. 适配器私有语义完成信号（semantic_done，由子类在 stdout 行流解析时置位）；
3. 总超时（timeout_seconds）。

裁决规则（照 interfaces.md §4）：
- 语义完成先到 → 给宽限期（默认 30s）等进程自己退；超期 kill，
  记 timed_out=False, semantic_done=True；
- 超时先到 → kill，记 timed_out=True；
- 自然退出先到 → 语义判定取信号位当前值（含"退出前最后一刻才打印
  result 行"的情况——先排干管道再判定，防止漏判）。

监视器做成通用基类：子类只提供 parse_line(line) -> SignalUpdate，
进程管理/竞速/原始输出落盘全部由本基类承担。平台：Windows 优先，
用 asyncio 进程的 kill()，不依赖 POSIX 进程组 API。
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from flowrun.adapters.registry import (
    INJECTION_MODE_CONTENT,
    INJECTION_MODE_PATH,
    WINDOWS_BATCH_SHIM_SUFFIXES,
    WINDOWS_COMMAND_SUFFIX_CANDIDATES,
    WINDOWS_POWERSHELL_SHIM_SUFFIXES,
    WINDOWS_SHIM_SUFFIXES,
    expand_command_template,
    resolve_command_path,
)

DEFAULT_SEMANTIC_GRACE_SECONDS = 30.0
# 进程死后排干管道的兜底时限：防极端场景（孤孙子进程仍握着管道写端）
# 让 dispatch 永久挂起。超期放弃排干，保留已写入的审计内容。
DEFAULT_PIPE_DRAIN_GRACE_SECONDS = 5.0
# taskkill /T /F 的自限时：绝不让收尸动作本身挂住 dispatch。
TASKKILL_TIMEOUT_SECONDS = 10.0


@dataclass
class DispatchResult:
    exit_code: int | None
    timed_out: bool
    semantic_done: bool      # 双信号：语义完成（如流式结果出现 end_turn/result 消息）
    stdout_path: Path       # 原始输出落盘（审计）
    duration_seconds: float
    # 以下为域C补充字段（interfaces.md §4 未列，任务书 T007 明确允许 structured_result）：
    structured_result: dict = field(default_factory=dict)  # 子类 parse_line 提取的结构化结果（如 session_id/成本）
    stderr_path: Path | None = None                        # stderr 审计文件路径（与 stdout_path 同名配对）


@dataclass
class SignalUpdate:
    """parse_line 的返回值：本行是否触发语义完成 + 本行提取出的结构化字段。"""

    semantic_done: bool = False
    structured_fields: dict = field(default_factory=dict)


class AgentAdapter(Protocol):
    name: str

    async def dispatch(self, packet_path: Path, agent_command: list[str], work_dir: Path,
                       timeout_seconds: float | None) -> DispatchResult: ...


class CommandHeadUnresolvableError(FileNotFoundError):
    """适配层错误：命令头在本机不可解析（PATH 与扩展名候选集都没命中）。

    继承 ``FileNotFoundError`` 是刻意的：win32 上 CreateProcess 找不到命令时抛的
    就是 ``FileNotFoundError: [WinError 2] 系统找不到指定的文件``，调用方现有的
    OSError 兜底路径不用改，而消息本身变成了能看懂的一句。
    """


def resolve_injection_mode(agent_command: list[str]) -> str:
    """决定 packet 的注入模式（registry.expand_command_template 的第 3 参）。

    **安全约束**：Windows 的 .cmd/.bat/.ps1 垫片都必须走 ``"path"`` 模式。
    垫片被重写为 ``cmd /c <shim> <args>`` / ``powershell -File <shim> <args>``
    后，整条命令行会被目标解释器重新分词，packet 正文里的 ``"``、``&``、``|``、
    ``^`` 会被当成命令分隔符/重定向执行（实测 ``do work " & echo PWNED &`` 会真
    跑第二条命令）。path 模式下正文一个字节都不进 argv，agent 自己去读文件路径。

    垫片判定与 ``plan_windows_command_launch`` 的启动方式共用
    ``WINDOWS_SHIM_SUFFIXES``：两边必须一致，否则会出现"按垫片投路径、却按普通
    命令启动"（或反之）的不变式裂缝——``.ps1`` 早先就漏在这一处。

    非垫片（.exe、未知命令、非 win32）保持 ``"content"`` 模式，行为完全不变。
    """
    resolved_path = resolve_command_path(agent_command[0]) if agent_command else None
    if resolved_path is None:
        return INJECTION_MODE_CONTENT
    if Path(resolved_path).suffix.lower() in WINDOWS_SHIM_SUFFIXES:
        return INJECTION_MODE_PATH
    return INJECTION_MODE_CONTENT


@dataclass(frozen=True)
class WindowsLaunchPlan:
    """win32 启动方式：直接 exec 的 argv，或交经 cmd.exe 的整条命令行（二选一）。

    批处理垫片（.cmd/.bat）只能走后者。``cmd /c <shim> <args>`` 的 argv 形态在
    "垫片路径含空格 + 参数含空格"时会产生 4 个引号，触发 cmd.exe 的引号剥离
    启发式：垫片根本没跑，rc=1、stdout 为空（实测）。整串形态下 CPython 的 Popen
    生成 ``cmd.exe /c "<list2cmdline 结果>"``，cmd 剥掉外层引号后正好是
    ``"<shim>" <args>`` —— 这才是 cmd 认可的形态（实测三种空格组合全部 rc=0）。
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
        return WindowsLaunchPlan(
            shell_command_line=subprocess.list2cmdline([resolved_path, *command[1:]])
        )
    if resolved_suffix in WINDOWS_POWERSHELL_SHIM_SUFFIXES:
        return WindowsLaunchPlan(exec_arguments=(
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", resolved_path, *command[1:],
        ))
    return WindowsLaunchPlan(exec_arguments=tuple(command))


def _taskkill_process_tree(process_id: int) -> bool:
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


class StreamMonitoringAdapter:
    """双信号退出监视器基类（子类只实现 parse_line）。

    dispatch 职责：占位符展开 → 以 work_dir 为 cwd 拉起子进程 → stdout 行流
    解析（子类语义）→ 三信号竞速裁决 → kill/收尸 → 原始 stdout/stderr
    全量落盘到 work_dir 审计文件。所有输入按不可信处理：packet/agent 输出
    只进结构化解析，绝不 eval/执行（interfaces.md §7）。
    """

    name: str = "stream-monitoring"
    command_heads: tuple[str, ...] = ()

    def __init__(self, semantic_grace_seconds: float = DEFAULT_SEMANTIC_GRACE_SECONDS,
                 pipe_drain_grace_seconds: float = DEFAULT_PIPE_DRAIN_GRACE_SECONDS):
        if semantic_grace_seconds < 0:
            raise ValueError("semantic_grace_seconds 必须 >= 0")
        self.semantic_grace_seconds = semantic_grace_seconds
        self.pipe_drain_grace_seconds = pipe_drain_grace_seconds
        self._dispatch_sequence = 0

    def parse_line(self, line: str) -> SignalUpdate:
        """子类覆写：对 stdout 一行做私有语义判定。默认不做任何判定。"""
        return SignalUpdate()

    async def dispatch(self, packet_path: Path, agent_command: list[str], work_dir: Path,
                       timeout_seconds: float | None) -> DispatchResult:
        # 先定注入模式（垫片 → 只给路径），再展开模板，最后解析启动方式：
        # 顺序不能反，否则正文已经躺在 cmd 的命令行里了。
        launch_plan = plan_windows_command_launch(
            expand_command_template(
                agent_command, packet_path, resolve_injection_mode(agent_command)
            )
        )
        work_dir.mkdir(parents=True, exist_ok=True)
        audit_basename = self._next_audit_basename(work_dir)

        started_at = time.monotonic()
        process = await self._spawn_child_process(launch_plan, work_dir)

        semantic_event = asyncio.Event()
        structured_result: dict = {}
        stdout_audit_file = (work_dir / f"{audit_basename}.stdout.log").open("wb")
        stderr_audit_file = (work_dir / f"{audit_basename}.stderr.log").open("wb")
        try:
            stdout_pump_task = asyncio.create_task(
                self._pump_stdout_lines(process, stdout_audit_file,
                                        semantic_event, structured_result),
                name=f"{self.name}-stdout-pump",
            )
            stderr_pump_task = asyncio.create_task(
                self._pump_stderr_bytes(process, stderr_audit_file),
                name=f"{self.name}-stderr-pump",
            )
            process_exit_task = asyncio.create_task(process.wait(),
                                                    name=f"{self.name}-exit-watch")
            semantic_signal_task = asyncio.create_task(semantic_event.wait(),
                                                       name=f"{self.name}-semantic-watch")
            overall_timeout_task = (
                asyncio.create_task(asyncio.sleep(timeout_seconds),
                                    name=f"{self.name}-overall-timeout")
                if timeout_seconds is not None else None
            )

            contenders = [process_exit_task, semantic_signal_task]
            if overall_timeout_task is not None:
                contenders.append(overall_timeout_task)
            done_contenders, _ = await asyncio.wait(
                contenders, return_when=asyncio.FIRST_COMPLETED
            )

            if process_exit_task in done_contenders:
                # 自然退出先到：exit_code 已在手；排干管道后再判语义信号，
                # 防 result 行还躺在管道缓冲里被漏判（进程已死 → 必然 EOF）。
                exit_code = process_exit_task.result()
                timed_out = False
                await self._drain_output_pipes([stdout_pump_task, stderr_pump_task])
                semantic_done = semantic_event.is_set()
            elif semantic_signal_task in done_contenders:
                # 语义完成先到（与超时同时到时语义优先，design.md §7
                # "语义完成信号优先"）：进入宽限期等进程自己退。
                exit_code = await self._await_exit_within_grace(process, process_exit_task)
                timed_out = False
                semantic_done = True
                await self._drain_output_pipes([stdout_pump_task, stderr_pump_task])
            else:
                # 总超时先到：连坐终止进程树。
                timed_out = True
                await self._kill_process_tree(process)
                exit_code = await process_exit_task
                await self._drain_output_pipes([stdout_pump_task, stderr_pump_task])
                semantic_done = semantic_event.is_set()

            await self._cancel_contenders([semantic_signal_task, overall_timeout_task])
        finally:
            stdout_audit_file.close()
            stderr_audit_file.close()

        return DispatchResult(
            exit_code=exit_code,
            timed_out=timed_out,
            semantic_done=semantic_done,
            stdout_path=work_dir / f"{audit_basename}.stdout.log",
            duration_seconds=time.monotonic() - started_at,
            structured_result=structured_result,
            stderr_path=work_dir / f"{audit_basename}.stderr.log",
        )

    def _next_audit_basename(self, work_dir: Path) -> str:
        """审计文件基名：<name>-dispatch-<时间戳>-<序号>，保证 work_dir 内唯一。"""
        self._dispatch_sequence += 1
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        return f"{self.name}-dispatch-{timestamp}-{self._dispatch_sequence:04d}"

    @staticmethod
    async def _spawn_child_process(launch_plan: WindowsLaunchPlan,
                                   work_dir: Path) -> asyncio.subprocess.Process:
        """按启动计划拉起子进程：批处理垫片经 cmd.exe，其余直接 exec。"""
        spawn_options = dict(
            cwd=work_dir,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if launch_plan.shell_command_line is not None:
            return await asyncio.create_subprocess_shell(
                launch_plan.shell_command_line, **spawn_options
            )
        assert launch_plan.exec_arguments is not None  # __post_init__ 保证二选一
        return await asyncio.create_subprocess_exec(
            *launch_plan.exec_arguments, **spawn_options
        )

    async def _pump_stdout_lines(self, process: asyncio.subprocess.Process,
                                 audit_file, semantic_event: asyncio.Event,
                                 structured_result: dict) -> None:
        """逐行读 stdout：原始字节全量落盘，同时喂子类 parse_line。"""
        assert process.stdout is not None
        async for raw_line in process.stdout:
            audit_file.write(raw_line)
            audit_file.flush()
            decoded_line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            signal_update = self.parse_line(decoded_line)
            if signal_update.structured_fields:
                structured_result.update(signal_update.structured_fields)
            if signal_update.semantic_done:
                semantic_event.set()

    async def _pump_stderr_bytes(self, process: asyncio.subprocess.Process,
                                 audit_file) -> None:
        """stderr 不做语义解析，原始字节全量落盘（审计）。"""
        assert process.stderr is not None
        async for raw_chunk in process.stderr:
            audit_file.write(raw_chunk)
            audit_file.flush()

    async def _await_exit_within_grace(self, process: asyncio.subprocess.Process,
                                       process_exit_task: asyncio.Task) -> int | None:
        """宽限期内等进程自然退出；超期连坐终止进程树（含垫片下的孙进程）。"""
        try:
            return await asyncio.wait_for(asyncio.shield(process_exit_task),
                                          timeout=self.semantic_grace_seconds)
        except asyncio.TimeoutError:
            await self._kill_process_tree(process)
            return await process_exit_task

    async def _kill_process_tree(self, process: asyncio.subprocess.Process) -> None:
        """终止子进程及其整棵进程树。

        垫片（``cmd /c shim``）下真 agent 是孙进程：只 TerminateProcess 直接
        子进程会让它成为孤儿，继续写 workspace（超时/宽限 kill 都会漏）。win32
        上改用 ``taskkill /T /F /PID``；taskkill 不可用或失败时回退
        ``process.kill()``。非 Windows 保持 ``process.kill()``（子进程栈无垫片）。
        """
        if sys.platform == "win32":
            killed_tree = await asyncio.to_thread(_taskkill_process_tree, process.pid)
            if killed_tree:
                return
        process.kill()

    async def _drain_output_pipes(self, pump_tasks: list[asyncio.Task]) -> None:
        """排干输出管道（正常路径：进程已死，很快 EOF）。超期放弃，保留已写审计。"""
        try:
            await asyncio.wait_for(asyncio.gather(*pump_tasks),
                                   timeout=self.pipe_drain_grace_seconds)
        except asyncio.TimeoutError:
            for pump_task in pump_tasks:
                pump_task.cancel()
            await asyncio.gather(*pump_tasks, return_exceptions=True)

    @staticmethod
    async def _cancel_contenders(contender_tasks: list[asyncio.Task | None]) -> None:
        """裁决后取消仍在等待的竞速方并收割，避免悬挂 task。"""
        for contender_task in contender_tasks:
            if contender_task is None or contender_task.done():
                continue
            contender_task.cancel()
            try:
                await contender_task
            except asyncio.CancelledError:
                pass
