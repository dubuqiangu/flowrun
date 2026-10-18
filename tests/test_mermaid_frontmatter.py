"""Tests for the frontmatter YAML subset of flowrun.compiler.frontmatter
(interfaces.md §2): extraction, strictness and numeric lower bounds."""

import sys
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import mermaid_parser_support

from mermaid_parser_support import parse_body  # noqa: E402  (负责把 src/ 加进 sys.path)

from flowrun.compiler.frontmatter import (  # noqa: E402
    FrontmatterError,
    parse_frontmatter_document,
)
from flowrun.compiler.mermaid_parser import (  # noqa: E402
    MermaidParseError,
    parse_flowchart,
)


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
