"""Tests for flowrun.compiler.mermaid_parser (tasks.md T002) and the
frontmatter YAML subset it consumes (interfaces.md §2)."""

import sys
import unittest
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from flowrun.compiler.frontmatter import (
    FrontmatterError,
    parse_frontmatter_document,
)
from flowrun.compiler.mermaid_parser import (
    MermaidParseError,
    parse_flowchart,
    split_agent_command,
)

SAMPLE_FRONTMATTER = """---
run: release-login
defaults: {agent: claude-code, budget_tokens: 1000000}
agents:
  claude-code: {cmd: "claude -p --output-format stream-json", resume: session}
  opencode: {cmd: "opencode run"}
---
"""


def parse_body(body_text: str):
    """Parse a flowchart body on top of the sample frontmatter."""
    return parse_flowchart(SAMPLE_FRONTMATTER + body_text)


class FrontmatterSubsetTest(unittest.TestCase):
    def test_sample_frontmatter_metadata_is_extracted(self):
        document = parse_body("flowchart TD\n  T001[任务] --> J001")
        self.assertEqual(document.run_name, "release-login")
        self.assertEqual(document.default_agent, "claude-code")
        self.assertEqual(document.budget_tokens, 1000000)
        self.assertEqual(
            document.agent_registry["claude-code"].command,
            ["claude", "-p", "--output-format", "stream-json"],
        )
        self.assertEqual(document.agent_registry["claude-code"].resume_mode, "session")
        self.assertEqual(document.agent_registry["opencode"].command, ["opencode", "run"])
        self.assertIsNone(document.agent_registry["opencode"].resume_mode)

    def test_inline_arrays_and_quoted_strings_are_parsed(self):
        frontmatter_result = parse_frontmatter_document(
            [
                (1, "run: array-run"),
                (2, 'tags: [alpha, "beta gamma", 42]'),
                (3, "quoted-key-example: 'single quoted'"),
            ]
        )
        self.assertEqual(
            frontmatter_result.document["tags"], ["alpha", "beta gamma", 42]
        )
        self.assertEqual(
            frontmatter_result.document["quoted-key-example"], "single quoted"
        )

    def test_second_level_indent_is_rejected(self):
        with self.assertRaises(FrontmatterError) as raised_context:
            parse_frontmatter_document(
                [
                    (1, "agents:"),
                    (2, "  claude-code:"),
                    (3, "    cmd: nested"),
                ]
            )
        self.assertIn("nested mappings are not supported", str(raised_context.exception))
        self.assertEqual(raised_context.exception.line_number, 2)

    def test_yaml_anchor_is_rejected(self):
        with self.assertRaises(FrontmatterError) as raised_context:
            parse_frontmatter_document([(1, "defaults: &anchor {agent: x}")])
        self.assertIn("anchors/aliases", str(raised_context.exception))
        self.assertEqual(raised_context.exception.line_number, 1)

    def test_block_scalar_is_rejected(self):
        with self.assertRaises(FrontmatterError) as raised_context:
            parse_frontmatter_document([(1, "description: |"), (2, "  text")])
        self.assertIn("block scalars", str(raised_context.exception))

    def test_tabs_are_rejected(self):
        with self.assertRaises(FrontmatterError) as raised_context:
            parse_frontmatter_document([(1, "run:\tbroken")])
        self.assertIn("tabs", str(raised_context.exception))

    def test_unterminated_frontmatter_block_is_rejected(self):
        with self.assertRaises(FrontmatterError) as raised_context:
            parse_flowchart("---\nrun: never-closed\nflowchart TD\n  A001[x]\n")
        self.assertIn("never closed", str(raised_context.exception))

    def test_unterminated_quote_is_rejected(self):
        with self.assertRaises(FrontmatterError) as raised_context:
            parse_frontmatter_document([(1, 'run: "unterminated')])
        self.assertIn("unterminated quoted string", str(raised_context.exception))


class NodeParsingTest(unittest.TestCase):
    def test_three_node_shapes_with_configs_and_line_numbers(self):
        document = parse_flowchart(
            "flowchart TD\n"
            "  T001[实现登录API;;agent=claude-code;;out=src/auth.py,test/auth.spec.ts]\n"
            "  D001{验收通过?}\n"
            "  H001([发布人审])\n"
        )
        task_node = document.nodes["T001"]
        self.assertEqual(task_node.shape, "task")
        self.assertEqual(task_node.label, "实现登录API")
        self.assertEqual(task_node.agent, "claude-code")
        self.assertEqual(task_node.outputs, ["src/auth.py", "test/auth.spec.ts"])
        self.assertEqual(task_node.line_number, 2)
        decision_node = document.nodes["D001"]
        self.assertEqual(decision_node.shape, "decision")
        self.assertEqual(decision_node.label, "验收通过?")
        self.assertEqual(decision_node.line_number, 3)
        human_node = document.nodes["H001"]
        self.assertEqual(human_node.shape, "human")
        self.assertEqual(human_node.label, "发布人审")
        self.assertIsNone(human_node.agent)
        self.assertIsNone(human_node.outputs)
        self.assertEqual(human_node.line_number, 4)

    def test_bare_id_in_edge_is_a_reference_until_defined(self):
        document = parse_flowchart(
            "flowchart TD\n  A001[任务] --> J001\n  J001[汇合点]\n"
        )
        self.assertIn("J001", document.nodes)
        self.assertEqual(document.nodes["J001"].label, "汇合点")
        self.assertEqual(len(document.edges), 1)
        self.assertEqual(document.edges[0].target_id, "J001")

    def test_standalone_bare_id_is_recorded_as_reference(self):
        document = parse_flowchart("flowchart TD\n  Z999\n")
        self.assertEqual(document.bare_references, {"Z999": 2})
        self.assertEqual(document.nodes, {})

    def test_semicolon_config_with_closing_bracket_is_rejected_with_line_number(self):
        with self.assertRaises(MermaidParseError) as raised_context:
            parse_flowchart(
                "flowchart TD\n"
                "  T001[修复登录;;out=src/auth]bad.py] --> A002[评审]\n"
            )
        self.assertIn("not allowed in M1", str(raised_context.exception))
        self.assertEqual(raised_context.exception.line_number, 2)

    def test_unknown_semicolon_config_key_is_rejected(self):
        with self.assertRaises(MermaidParseError) as raised_context:
            parse_flowchart("flowchart TD\n  T001[任务;;colour=red]\n")
        self.assertIn("unknown ';;' config key 'colour'", str(raised_context.exception))

    def test_duplicate_node_definition_is_rejected(self):
        with self.assertRaises(MermaidParseError) as raised_context:
            parse_flowchart(
                "flowchart TD\n  T001[第一个定义]\n  T001[第二个定义]\n"
            )
        self.assertIn("defined twice", str(raised_context.exception))
        self.assertEqual(raised_context.exception.line_number, 3)

    def test_unrecognized_node_shape_is_rejected(self):
        with self.assertRaises(MermaidParseError) as raised_context:
            parse_flowchart("flowchart TD\n  A001((round shape))\n")
        self.assertIn("unrecognized node shape", str(raised_context.exception))


class EdgeParsingTest(unittest.TestCase):
    def test_arrow_variants_and_labels(self):
        document = parse_flowchart(
            "flowchart TD\n"
            "  A001[任务一] --> B001[任务二]\n"
            "  B001 ---> C001[任务三]\n"
            "  C001 --- D001[任务四]\n"
            "  D001 -->|when: all| E001[任务五]\n"
            "  E001 -->|when: fail, retry: 2| F001[任务六]\n"
            "  F001 -->|timeout: 30m| G001[任务七]\n"
            "  G001 -->|timeout: 45s| H001([任务八])\n"
            "  H001 -->|timeout: 2h| I001[任务九]\n"
            "  I001 -->|timeout: 90| J002[任务十]\n"
            "  J002 --> K001[链一] --> L001[链二]\n"
        )
        edges = document.edges
        self.assertEqual(len(edges), 11)
        by_source = {(edge.source_id, edge.target_id): edge for edge in edges}
        plain_arrow = by_source[("A001", "B001")]
        self.assertIsNone(plain_arrow.when)
        self.assertEqual(plain_arrow.retry, 0)
        self.assertIsNone(plain_arrow.timeout_seconds)
        self.assertEqual(plain_arrow.line_number, 2)
        self.assertIsNone(by_source[("B001", "C001")].when)  # ---> variant
        self.assertIsNone(by_source[("C001", "D001")].when)  # --- open edge
        self.assertEqual(by_source[("D001", "E001")].when, "all")
        fail_edge = by_source[("E001", "F001")]
        self.assertEqual(fail_edge.when, "fail")
        self.assertEqual(fail_edge.retry, 2)
        self.assertEqual(by_source[("F001", "G001")].timeout_seconds, 1800.0)
        self.assertEqual(by_source[("G001", "H001")].timeout_seconds, 45.0)
        self.assertEqual(by_source[("H001", "I001")].timeout_seconds, 7200.0)
        self.assertEqual(by_source[("I001", "J002")].timeout_seconds, 90.0)
        # chained line produces two edges on the same source line
        self.assertIn(("J002", "K001"), by_source)
        self.assertIn(("K001", "L001"), by_source)
        self.assertEqual(by_source[("K001", "L001")].line_number, 11)

    def test_unknown_edge_label_key_is_rejected_with_line_number(self):
        with self.assertRaises(MermaidParseError) as raised_context:
            parse_flowchart("flowchart TD\n  A001[x] -->|colour: red| B001[y]\n")
        self.assertIn("unknown edge label key 'colour'", str(raised_context.exception))
        self.assertEqual(raised_context.exception.line_number, 2)

    def test_invalid_retry_count_is_rejected(self):
        with self.assertRaises(MermaidParseError) as raised_context:
            parse_flowchart("flowchart TD\n  A001[x] -->|retry: two| B001[y]\n")
        self.assertIn("invalid retry count", str(raised_context.exception))

    def test_invalid_timeout_duration_is_rejected(self):
        with self.assertRaises(MermaidParseError) as raised_context:
            parse_flowchart("flowchart TD\n  A001[x] -->|timeout: soon| B001[y]\n")
        self.assertIn("invalid timeout duration", str(raised_context.exception))

    def test_empty_edge_label_is_rejected(self):
        with self.assertRaises(MermaidParseError) as raised_context:
            parse_flowchart("flowchart TD\n  A001[x] -->|| B001[y]\n")
        self.assertIn("empty", str(raised_context.exception))

    def test_dotted_arrow_is_rejected(self):
        with self.assertRaises(MermaidParseError) as raised_context:
            parse_flowchart("flowchart TD\n  A001[x] -.-> B001[y]\n")
        self.assertIn("unexpected text after node 'A001'", str(raised_context.exception))
        self.assertEqual(raised_context.exception.line_number, 2)


class LineClassificationTest(unittest.TestCase):
    def test_subgraph_lines_are_ignored_but_inner_nodes_parsed(self):
        document = parse_flowchart(
            "flowchart TD\n"
            "  subgraph 准备阶段\n"
            "    T001[实现登录API]\n"
            "  end\n"
            "  T001 --> D001{验收通过?}\n"
        )
        self.assertNotIn("subgraph", str(document.nodes))
        self.assertEqual(sorted(document.nodes), ["D001", "T001"])
        self.assertEqual(len(document.edges), 1)

    def test_comments_are_ignored(self):
        document = parse_flowchart(
            "flowchart TD\n"
            "  %% full line comment\n"
            "  A001[任务] --> B001[评审] %% trailing comment\n"
        )
        self.assertEqual(sorted(document.nodes), ["A001", "B001"])
        self.assertEqual(len(document.edges), 1)

    def test_sequence_diagram_is_rejected_explicitly(self):
        with self.assertRaises(MermaidParseError) as raised_context:
            parse_flowchart("sequenceDiagram\n  A->>B: 你好\n")
        self.assertIn("sequenceDiagram has no executable semantics", str(raised_context.exception))
        self.assertEqual(raised_context.exception.line_number, 1)

    def test_missing_declaration_is_rejected(self):
        with self.assertRaises(MermaidParseError) as raised_context:
            parse_flowchart("  A001[x] --> B001[y]\n")
        self.assertIn("expected 'flowchart TD' or 'flowchart LR'", str(raised_context.exception))
        self.assertEqual(raised_context.exception.line_number, 1)

    def test_invalid_direction_is_rejected(self):
        with self.assertRaises(MermaidParseError) as raised_context:
            parse_flowchart("flowchart XX\n  A001[x]\n")
        self.assertIn("invalid flowchart declaration", str(raised_context.exception))

    def test_flowchart_lr_direction_is_accepted(self):
        document = parse_flowchart("flowchart LR\n  A001[x] --> B001[y]\n")
        self.assertEqual(document.declaration_line_number, 1)
        self.assertEqual(len(document.edges), 1)

    def test_edge_arrow_missing_source_is_rejected(self):
        with self.assertRaises(MermaidParseError) as raised_context:
            parse_flowchart("flowchart TD\n  --> B001[y]\n")
        self.assertIn("missing its source node", str(raised_context.exception))


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


class FrontmatterStrictnessTest(unittest.TestCase):
    """FIX-5/6/7：引号后残余、重复键、行内注释尾巴。"""

    def test_trailing_text_after_quoted_scalar_is_rejected(self):
        with self.assertRaises(FrontmatterError) as raised_context:
            parse_frontmatter_document([(1, 'run: "release" trailing junk')])
        self.assertIn("after the closing quote", str(raised_context.exception))
        self.assertEqual(raised_context.exception.line_number, 1)

    def test_escaped_quote_does_not_silently_truncate(self):
        with self.assertRaises(FrontmatterError) as raised_context:
            parse_frontmatter_document([(1, r'run: "re\"lease x"')])
        self.assertIn("after the closing quote", str(raised_context.exception))
        self.assertEqual(raised_context.exception.line_number, 1)

    def test_trailing_comment_after_quoted_scalar_is_allowed(self):
        frontmatter_result = parse_frontmatter_document([(1, 'run: "release" # 注')])
        self.assertEqual(frontmatter_result.document["run"], "release")

    def test_duplicate_top_level_key_is_rejected(self):
        with self.assertRaises(FrontmatterError) as raised_context:
            parse_frontmatter_document([(1, "run: one"), (2, "run: two")])
        self.assertIn("duplicate", str(raised_context.exception))
        self.assertEqual(raised_context.exception.line_number, 2)

    def test_duplicate_section_is_rejected_instead_of_silently_resetting(self):
        with self.assertRaises(FrontmatterError) as raised_context:
            parse_frontmatter_document(
                [
                    (1, "agents:"),
                    (2, "  first: {cmd: a}"),
                    (3, "agents:"),
                    (4, "  second: {cmd: b}"),
                ]
            )
        self.assertIn("duplicate", str(raised_context.exception))
        self.assertEqual(raised_context.exception.line_number, 3)

    def test_duplicate_key_inside_section_is_rejected(self):
        with self.assertRaises(FrontmatterError) as raised_context:
            parse_frontmatter_document(
                [(1, "agents:"), (2, "  first: {cmd: a}"), (3, "  first: {cmd: b}")]
            )
        self.assertIn("duplicate", str(raised_context.exception))
        self.assertEqual(raised_context.exception.line_number, 3)

    def test_inline_comment_tail_is_stripped_from_bare_scalar(self):
        frontmatter_result = parse_frontmatter_document([(1, "run: release # draft")])
        self.assertEqual(frontmatter_result.document["run"], "release")

    def test_hash_inside_quotes_is_not_stripped(self):
        frontmatter_result = parse_frontmatter_document([(1, 'run: "release # draft"')])
        self.assertEqual(frontmatter_result.document["run"], "release # draft")


class WhenBarrierCaseTest(unittest.TestCase):
    """FIX-8：when 值大小写不敏感地归一为 'all'。"""

    def test_uppercase_when_all_is_normalized_to_lowercase(self):
        document = parse_body("flowchart TD\n  A001[任务] -->|when: ALL| B001[评审]\n")
        self.assertEqual(document.edges[0].when, "all")

    def test_mixed_case_when_all_is_normalized_to_lowercase(self):
        document = parse_body("flowchart TD\n  A001[任务] -->|when: All| B001[评审]\n")
        self.assertEqual(document.edges[0].when, "all")

    def test_natural_language_predicate_keeps_original_text(self):
        document = parse_body("flowchart TD\n  A001[任务] -->|when: Fail-Fast| B001[评审]\n")
        self.assertEqual(document.edges[0].when, "Fail-Fast")


class SubgraphStructureTest(unittest.TestCase):
    """FIX-12：未闭合 subgraph 必须报错（带 subgraph 起始行）。"""

    def test_unterminated_subgraph_is_rejected_with_opening_line(self):
        with self.assertRaises(MermaidParseError) as raised_context:
            parse_flowchart(
                "flowchart TD\n"
                "  subgraph 准备阶段\n"
                "    T001[实现登录API]\n"
            )
        self.assertIn("never closed", str(raised_context.exception))
        self.assertEqual(raised_context.exception.line_number, 2)

    def test_closed_subgraph_still_parses(self):
        document = parse_flowchart(
            "flowchart TD\n  subgraph 准备阶段\n    T001[x]\n  end\n  T001 --> B001[y]\n"
        )
        self.assertEqual(sorted(document.nodes), ["B001", "T001"])


class NumericLowerBoundTest(unittest.TestCase):
    """FIX-13：budget_tokens / timeout 必须为正数。"""

    def test_non_positive_budget_tokens_is_rejected(self):
        source_text = (
            "---\n"
            "run: test-run\n"
            "defaults: {agent: tester, budget_tokens: -5}\n"
            "agents:\n"
            "  tester: {cmd: \"fake-agent run\"}\n"
            "---\n"
            "flowchart TD\n"
            "  A001[任务a;;out=a.txt]\n"
        )
        with self.assertRaises(MermaidParseError) as raised_context:
            parse_flowchart(source_text)
        self.assertIn("budget_tokens", str(raised_context.exception))
        self.assertIn("greater than 0", str(raised_context.exception))

    def test_zero_budget_tokens_is_rejected(self):
        source_text = (
            "---\n"
            "run: test-run\n"
            "defaults: {agent: tester, budget_tokens: 0}\n"
            "agents:\n"
            "  tester: {cmd: \"fake-agent run\"}\n"
            "---\n"
            "flowchart TD\n"
            "  A001[任务a;;out=a.txt]\n"
        )
        with self.assertRaises(MermaidParseError) as raised_context:
            parse_flowchart(source_text)
        self.assertIn("budget_tokens", str(raised_context.exception))

    def test_zero_timeout_is_rejected(self):
        with self.assertRaises(MermaidParseError) as raised_context:
            parse_flowchart("flowchart TD\n  A001[x] -->|timeout: 0| B001[y]\n")
        self.assertIn("greater than 0", str(raised_context.exception))
        self.assertEqual(raised_context.exception.line_number, 2)

    def test_zero_second_timeout_is_rejected(self):
        with self.assertRaises(MermaidParseError) as raised_context:
            parse_flowchart("flowchart TD\n  A001[x] -->|timeout: 0s| B001[y]\n")
        self.assertIn("greater than 0", str(raised_context.exception))


if __name__ == "__main__":
    unittest.main()
