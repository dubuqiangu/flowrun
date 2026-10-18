"""Tests for split_agent_command (flowrun.compiler.mermaid_parser): win32 切分语义。

覆盖 FIX-3（posix shlex 会吃掉 Windows 路径反斜杠）与 C4 回归
（win32 必须是 CommandLineToArgvW 语义，而非"非 POSIX shlex + 手工剥引号"）。
"""

import sys
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
SOURCE_ROOT = TESTS_DIRECTORY.parent / "src"
for path_entry in (str(SOURCE_ROOT), str(TESTS_DIRECTORY)):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)

from flowrun.compiler.mermaid_parser import (  # noqa: E402
    MermaidParseError,
    parse_flowchart,
    split_agent_command,
)


class AgentCommandSplittingTest(unittest.TestCase):
    """FIX-3：win32 下 shlex 的 posix 模式会吃掉路径反斜杠。"""

    def test_windows_split_keeps_backslashes_and_strips_paired_quotes(self):
        command_words = split_agent_command(
            r'"C:\Program Files\agent\cli.cmd" -p', platform="win32"
        )
        self.assertEqual(command_words, [r"C:\Program Files\agent\cli.cmd", "-p"])

    def test_windows_split_keeps_unquoted_backslashes(self):
        # posix=True 会把 C:\agent\cli.cmd 吃成 C:agentcli.cmd
        self.assertEqual(
            split_agent_command(r"C:\agent\cli.cmd -p", platform="win32"),
            [r"C:\agent\cli.cmd", "-p"],
        )

    def test_windows_split_handles_quoted_path_with_trailing_backslash(self):
        # posix=True 会把 \" 当转义引号 → "No closing quotation"
        self.assertEqual(
            split_agent_command(r'"C:\agent dir\" -p', platform="win32"),
            ["C:\\agent dir\\", "-p"],
        )

    def test_non_windows_split_uses_posix_rules(self):
        self.assertEqual(
            split_agent_command("agent 'a b' -p", platform="linux"),
            ["agent", "a b", "-p"],
        )

    @unittest.skipUnless(sys.platform == "win32", "win32 默认分支专属")
    def test_frontmatter_cmd_with_windows_path_is_not_mangled(self):
        document = parse_flowchart(
            "---\n"
            "run: win-run\n"
            "defaults: {agent: win}\n"
            "agents:\n"
            '  win: {cmd: \'"C:\\Program Files\\agent\\cli.cmd" -p\'}\n'
            "---\n"
            "flowchart TD\n"
            "  A001[任务]\n"
        )
        self.assertEqual(
            document.agent_registry["win"].command,
            [r"C:\Program Files\agent\cli.cmd", "-p"],
        )


class WindowsCommandWordSplittingTest(unittest.TestCase):
    """C4 回归：win32 切分必须是 CommandLineToArgvW 语义，不是"非 POSIX shlex +
    手工剥引号"。旧实现把 ``--flag="a b"`` 切成 ``['--flag="a', 'b"']``、
    把 ``""`` 切成空元素、把 ``&`` 当独立参数。"""

    def test_quoted_value_inside_a_flag_is_joined(self):
        self.assertEqual(
            split_agent_command(
                'claude -p --flag="a b" end', platform="win32"
            ),
            ["claude", "-p", "--flag=a b", "end"],
        )

    def test_empty_quoted_word_is_dropped(self):
        self.assertEqual(
            split_agent_command('prog a "" d', platform="win32"),
            ["prog", "a", "d"],
        )
        self.assertEqual(
            split_agent_command('prog a ""', platform="win32"), ["prog", "a"]
        )

    def test_repeated_spaces_and_tabs_do_not_create_empty_words(self):
        self.assertEqual(
            split_agent_command("prog   a\tb", platform="win32"),
            ["prog", "a", "b"],
        )

    def test_shell_metacharacters_stay_literal_words(self):
        self.assertEqual(
            split_agent_command("prog a & b", platform="win32"),
            ["prog", "a", "&", "b"],
        )
        self.assertEqual(
            split_agent_command("prog a ; b", platform="win32"),
            ["prog", "a", ";", "b"],
        )
        self.assertEqual(
            split_agent_command("prog a | b", platform="win32"),
            ["prog", "a", "|", "b"],
        )
        # %VAR% 不得被展开，也不得被当成单独的分隔符
        self.assertEqual(
            split_agent_command("prog a %VAR%", platform="win32"),
            ["prog", "a", "%VAR%"],
        )

    def test_single_quotes_are_plain_characters(self):
        self.assertEqual(
            split_agent_command("agent 'a b' -p", platform="win32"),
            ["agent", "'a", "b'", "-p"],
        )

    def test_unterminated_quote_still_raises(self):
        for command_text in ('"unterminated', 'prog "unterminated', 'prog a "x'):
            with self.subTest(command_text=command_text):
                with self.assertRaises(ValueError):
                    split_agent_command(command_text, platform="win32")

    def test_blank_command_text_yields_no_words(self):
        self.assertEqual(split_agent_command("", platform="win32"), [])
        self.assertEqual(split_agent_command("   \t ", platform="win32"), [])

    @unittest.skipUnless(sys.platform == "win32", "win32 原生分支专属")
    def test_hand_written_fallback_agrees_with_native_command_line_to_argv(self):
        from flowrun.compiler.mermaid_parser import (
            _split_windows_command_line,
            _split_with_native_command_line_to_argv,
        )

        for command_text in (
            'claude -p --flag="a b" end',
            'prog a "" d',
            "prog a & b",
            "prog a %VAR%",
            r'"C:\Program Files\agent\cli.cmd" -p',
            r"C:\agent\cli.cmd -p",
            r'"C:\agent dir\" -p',
            "prog   a\tb",
            "agent 'a b' -p",
        ):
            with self.subTest(command_text=command_text):
                native_words = _split_with_native_command_line_to_argv(command_text)
                self.assertIsNotNone(native_words)
                self.assertEqual(
                    _split_windows_command_line(command_text),
                    [word for word in native_words if word],
                )

    def test_frontmatter_reports_unparsable_quoted_command(self):
        with self.assertRaises(MermaidParseError) as raised_context:
            parse_flowchart(
                "---\n"
                "run: win-run\n"
                "defaults: {agent: win}\n"
                "agents:\n"
                '  win: {cmd: \'cli -p "unterminated\'}\n'
                "---\n"
                "flowchart TD\n"
                "  A001[任务]\n"
            )
        self.assertIn("unparsable cmd", str(raised_context.exception))


if __name__ == "__main__":
    unittest.main()
