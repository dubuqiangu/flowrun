"""FlowRun 域A · agent 命令模板分词（win32 = ``CommandLineToArgvW`` 语义）。

frontmatter 的 ``cmd:`` 是**命令模板**而非 shell 命令行，故不能直接套 POSIX 的
``shlex``：POSIX 规则把反斜杠当转义符，会吃掉 Windows 路径（``shlex.split('C:\\agent\\cli.cmd -p')``
得到 ``['C:agentcli.cmd', '-p']``），带尾反斜杠的引号路径还会被判"引号未闭合"。

win32 一律按 Windows 命令行规则切词：引号是分隔符而非字符、``\\"`` 是转义引号、
``&`` / ``;`` / ``|`` / ``%`` / ``'`` 都是普通字符；空词丢弃；未闭合引号报错。
非 win32 沿用 ``shlex.split``（POSIX 规则），行为不变。

## 与原生实现的一致性契约（本模块的语义边界）

``CommandLineToArgvW`` 对**病态引号序列**有一套实现细节，手写回退实现无法逐例复现
（实测：一个词内出现连续 4 个引号、且中间没有空白时，原生会在词内切出两个元素，
如 ``['a', '"b']``；``prog`` 后接连续 4 个引号会得到 ``['prog', '"']``；首词含内部
引号时原生同样按引号语义剥离——与 MS 文档描述的 argv[0] 特例并不一致）。因此本模块
采取**"要么与原生一致、要么显式报错"**：

1. win32 且原生可用 → 同时运行原生与手写实现，**二者结果不一致即抛 ValueError**
   （fail-loud，绝不静默返回一个与操作系统不同的切分）；
2. 手写回退自身对**已知无法忠实复现**的形式（引号段内的 ``""``）直接抛 ValueError。
   实测随机病态语料 28687 例：无此护栏时静默分歧 3567 例，加护栏后降到 2730 例
   （护栏只堵住已知形式，**并未**使回退逐例安全）。因此第 1 条的原生交叉校验才是
   win32 上"零静默分歧"的真正来源；**原生不可用的降级环境仍有约 9.5% 的病态输入
   会被静默误切**——这是本模块记录在案的已知限制（修复需完整复刻
   CommandLineToArgvW，非本规模的任务）；
3. 非 win32 → ``shlex.split``（POSIX 规则），不参与上述校验。

空参数丢弃是**既有语义**（``prog a "" d`` → ``['prog', 'a', 'd']``）：领而未用的空串
不构成 argv 元素；将来若需传递真正的空参数，须作为独立任务改本契约。

本模块从 ``flowrun.compiler.mermaid_parser`` 拆出（纯搬移 + 上述 fail-loud 契约），
``flowrun.compiler.mermaid_parser.split_agent_command`` 仍可导入（再导出）。
"""
from __future__ import annotations

import shlex
import sys

__all__ = ["split_agent_command"]


def split_agent_command(command_text: str, platform: str = sys.platform) -> list[str]:
    """Split a frontmatter ``cmd:`` template into argv words.

    POSIX shlex rules treat a backslash as an escape character, which destroys
    Windows paths: ``shlex.split('C:\\agent\\cli.cmd -p')`` yields
    ``['C:agentcli.cmd', '-p']``, and a quoted path ending in a backslash
    (``"C:\\agent dir\\"``) is reported as an unterminated quote. A non-POSIX
    shlex with hand-stripped quotes is no better: it splits ``--flag="a b"``
    into ``['--flag="a', 'b"']`` and turns ``a & b`` into three words.

    On win32 we therefore follow the Windows command-line rules
    (CommandLineToArgvW semantics): a quote is a delimiter rather than part of
    a word, ``\\"`` is an escaped quote, and ``&``, ``;``, ``|``, ``%``, ``'``
    are plain characters. win32 + ctypes asks ``shell32.CommandLineToArgvW``
    directly; otherwise the documented ``_split_windows_command_line`` fallback
    implements the same rules. Both drop empty words and both make an
    unterminated quote raise ValueError (callers turn it into MermaidParseError).

    Divergence is never silent while the native call is available (module
    docstring): when it disagrees with the fallback, this raises ValueError
    instead of returning one of the two answers. Without ctypes it falls back to
    the hand-written splitter, whose residual divergence is a documented
    limitation (module docstring point 2). The non-win32 branch keeps plain
    ``shlex.split`` (POSIX rules) unchanged.
    """
    if platform != "win32":
        return shlex.split(command_text)
    if not command_text.strip():
        return []
    hand_parsed_words = _split_windows_command_line(command_text)
    native_words = _split_with_native_command_line_to_argv(command_text)
    if native_words is None:
        return hand_parsed_words
    non_empty_native_words = [word for word in native_words if word]
    if non_empty_native_words != hand_parsed_words:
        raise ValueError(
            "command template is split differently by the OS and by the fallback "
            f"parser; refusing to guess which is right: {command_text!r} "
            f"(native={non_empty_native_words!r}, fallback={hand_parsed_words!r})"
        )
    return non_empty_native_words


def _split_with_native_command_line_to_argv(command_text: str) -> list[str] | None:
    """win32: defer to shell32.CommandLineToArgvW; None when ctypes is unusable."""
    try:
        import ctypes
    except ImportError:
        return None
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None
    command_line_to_argv = windll.shell32.CommandLineToArgvW
    # restype 必须显式指定：默认的 c_int 会在 64 位上截断返回的指针。
    command_line_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argument_count = ctypes.c_int(0)
    argument_vector = command_line_to_argv(
        ctypes.c_wchar_p(command_text), ctypes.byref(argument_count)
    )
    if not argument_vector:
        return None
    try:
        return [argument_vector[index] for index in range(argument_count.value)]
    finally:
        windll.kernel32.LocalFree(argument_vector)


def _split_windows_command_line(command_text: str) -> list[str]:
    """Documented CommandLineToArgvW-compatible splitter (fallback + validator).

    Rules (Microsoft, "Parsing C++ Command-Line Arguments"):
    - the first word (argv[0]) is special: it is literal up to the first
      whitespace unless it *starts* with a quote, in which case it is quoted
      with backslashes kept literal (so ``"C:\\agent dir\\" -p`` is that path
      plus ``-p``);
    - later words: ``2n`` backslashes before a quote collapse to ``n`` and the
      quote delimits, ``2n+1`` collapse to ``n`` plus one literal quote;
    - space and tab separate words only outside quotes; every other character
      (``&``, ``;``, ``|``, ``%``, ``'``) is a literal;
    - a word made of nothing but quotes is empty and therefore dropped;
    - an unterminated quote raises ValueError.

    Fail-loud boundary: the ``""``-inside-a-quoted-section form (which
    CommandLineToArgvW renders as a literal quote and can also split a word)
    cannot be reproduced faithfully here, so it raises ValueError instead of
    returning a different split. Callers on win32-with-ctypes additionally
    cross-check against the native call (see ``split_agent_command``).
    """
    words: list[str] = []
    is_first_word = True
    index = 0
    text_length = len(command_text)
    while index < text_length:
        while index < text_length and command_text[index] in " \t":
            index += 1
        if index >= text_length:
            break
        word_characters: list[str] = []
        if is_first_word:
            if command_text[index] == '"':
                index += 1
                while index < text_length and command_text[index] != '"':
                    word_characters.append(command_text[index])
                    index += 1
                if index >= text_length:
                    raise ValueError(
                        f"unterminated quote in command template {command_text!r}"
                    )
                index += 1  # 结束引号
            else:
                while index < text_length and command_text[index] not in " \t":
                    word_characters.append(command_text[index])
                    index += 1
            is_first_word = False
        else:
            inside_quotes = False
            while index < text_length:
                current_character = command_text[index]
                if current_character == "\\":
                    backslash_run_start = index
                    while index < text_length and command_text[index] == "\\":
                        index += 1
                    backslash_count = index - backslash_run_start
                    if index < text_length and command_text[index] == '"':
                        word_characters.extend("\\" * (backslash_count // 2))
                        if backslash_count % 2 == 1:
                            word_characters.append('"')
                        else:
                            inside_quotes = not inside_quotes
                        index += 1
                    else:
                        word_characters.extend("\\" * backslash_count)
                    continue
                if current_character == '"':
                    if (
                        inside_quotes
                        and index + 1 < text_length
                        and command_text[index + 1] == '"'
                    ):
                        raise ValueError(
                            "command template uses the \"\"-inside-a-quoted-section "
                            "form, which this fallback cannot reproduce faithfully: "
                            f"{command_text!r}"
                        )
                    inside_quotes = not inside_quotes
                    index += 1
                    continue
                if not inside_quotes and current_character in " \t":
                    break
                word_characters.append(current_character)
                index += 1
            if inside_quotes:
                raise ValueError(
                    f"unterminated quote in command template {command_text!r}"
                )
        word = "".join(word_characters)
        if word:
            words.append(word)
    return words
