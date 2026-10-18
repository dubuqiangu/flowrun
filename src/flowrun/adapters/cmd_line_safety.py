"""cmd.exe 命令行注入安全：集中的参数渲染 + "无法安全表达即拒绝"规则。

背景（实测取证）：把 packet 文件路径或模板参数嵌进 cmd.exe 命令行（Windows
``.cmd``/``.bat`` 垫片分支经 ``cmd /c`` 启动）时，cmd.exe 会做两件危险的事：

1. ``%NAME%`` 环境变量展开——**即使写在双引号内也会展开**，所以"加引号"无法
   中和它；``%%`` 也不是命令行层面的可靠转义（它只在批处理文件**内部**表示
   字面 ``%``）。因此含 ``%`` 的路径/参数一律**拒绝执行**（失败关闭），而不是
   硬拼一个并不可靠的转义。
2. 命令分隔符重分词——``&`` / ``|`` / ``^`` / ``<`` / ``>`` / ``(`` / ``)``
   在**不在引号内**时被当成控制符。``subprocess.list2cmdline`` 只在参数含
   空白/引号时才加引号，于是裸的 ``a&b`` 会被劈成两条命令执行。故本模块对
   每个参数**无条件**加双引号（引号内的所有元字符都失活）。

含 ``"``（Windows 路径本就非法）与含 ``\\r`` / ``\\n``（会截断/注入命令行）
的参数同样拒绝。

边界：直接在 ``CreateProcess`` 上拉起的进程（不经 cmd.exe，如 ``.exe`` 与
``powershell -File``）**不做**此限制——那里 ``%`` 是字面量，误伤反而是缺陷。
"""
from __future__ import annotations

from collections.abc import Iterable

# cmd.exe 命令行里无法安全表达的字符 -> 原因（用于诊断文案，顺序即报告优先级）。
_CMD_EXE_UNSAFE_CHARACTER_REASONS: dict[str, str] = {
    "%": "cmd.exe 会把它当环境变量展开（即使位于双引号内也照展开，"
         "加引号或写成 '%%' 都不是命令行层面的可靠转义）",
    '"': "它是 cmd.exe 的引号字符，无法安全嵌入（Windows 文件名也禁止它）",
    "\r": "它是回车控制字符，会把命令行截断/注入成多条",
    "\n": "它是换行控制字符，会把命令行截断/注入成多条",
}


class CmdExeUnsafeArgumentError(ValueError):
    """参数无法安全嵌入 cmd.exe 命令行（失败关闭：拒绝执行，不硬拼转义）。"""


def require_cmd_exe_safe_argument(argument: str, *, context: str = "参数") -> None:
    """校验单个参数可安全嵌入 cmd.exe 命令行；不安全则抛异常（见模块 docstring）。"""
    for unsafe_character, reason in _CMD_EXE_UNSAFE_CHARACTER_REASONS.items():
        if unsafe_character in argument:
            raise CmdExeUnsafeArgumentError(
                f"无法安全地把{context} {argument!r} 交给 cmd.exe：含 "
                f"{unsafe_character!r}——{reason}。拒绝执行；请把工作区"
                f"（packet 与输出所在目录）移到不含 {unsafe_character!r} 的路径，"
                "或修正命令模板里的该参数。"
            )


def render_cmd_exe_arguments(arguments: Iterable[str]) -> str:
    """把参数渲染成 cmd.exe 命令行：每个参数**无条件**双引号包裹，空格连接。

    无条件包裹（而非 ``list2cmdline`` 的"仅含空白/引号时才加引号"）是关键：
    后者会让裸 ``&`` / ``|`` / ``^`` / ``<`` / ``>`` 漏在引号外，被 cmd.exe 当
    控制符重新分词。引号内的所有元字符都是字面量，故一律包裹。空参数渲染为
    ``""``。含 ``%`` / ``"`` / ``\\r`` / ``\\n`` 的参数在此失败关闭（见
    :func:`require_cmd_exe_safe_argument`）。
    """
    rendered_arguments = []
    for argument in arguments:
        require_cmd_exe_safe_argument(argument)
        rendered_arguments.append(f'"{argument}"')
    return " ".join(rendered_arguments)
