"""Tests for the compile-time numeric caps in flowrun.compiler.limits.

Each cap is covered twice: a value above the cap is a compile error with a
source line number, and a value exactly at the cap still compiles. The caps
live in one module so the parser and the validator cannot drift apart.
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from flowrun.compiler.limits import (
    MAX_BUDGET_TOKENS,
    MAX_RETRY_ATTEMPTS,
    MAX_TIMEOUT_SECONDS,
)
from flowrun.compiler.mermaid_parser import MermaidParseError
from flowrun.compiler.validator import compile_flow_source

TEST_FRONTMATTER = """---
run: test-run
defaults: {agent: tester, budget_tokens: 5000}
agents:
  tester: {cmd: "fake-agent run"}
---
"""


def compile_body(body_text: str):
    return compile_flow_source(TEST_FRONTMATTER + body_text)


def compile_with_budget(budget_tokens: int, body_text: str):
    return compile_flow_source(
        "---\n"
        "run: test-run\n"
        f"defaults: {{agent: tester, budget_tokens: {budget_tokens}}}\n"
        "agents:\n"
        '  tester: {cmd: "fake-agent run"}\n'
        "---\n" + body_text
    )


class TimeoutUpperBoundTest(unittest.TestCase):
    """timeout: must be finite and at most MAX_TIMEOUT_SECONDS."""

    def test_timeout_above_max_is_rejected(self):
        boundary_hours = MAX_TIMEOUT_SECONDS // 3600
        with self.assertRaises(MermaidParseError) as raised_context:
            compile_body(
                "flowchart TD\n"
                f"  A001[任务a;;out=a.txt] -->|timeout: {boundary_hours + 1}h| "
                "B001[任务b;;out=b.txt]\n"
            )
        self.assertIn("exceeds the maximum", str(raised_context.exception))
        self.assertEqual(raised_context.exception.line_number, 8)

    def test_timeout_at_max_is_accepted(self):
        boundary_hours = MAX_TIMEOUT_SECONDS // 3600
        flow_ir = compile_body(
            "flowchart TD\n"
            f"  A001[任务a;;out=a.txt] -->|timeout: {boundary_hours}h| "
            "B001[任务b;;out=b.txt]\n"
        )
        self.assertEqual(flow_ir.edges[0].timeout_seconds, float(MAX_TIMEOUT_SECONDS))

    def test_overflowing_timeout_is_rejected(self):
        # 400 digits overflow float() to inf; an infinite timeout never fires.
        overflow_digits = "9" * 400
        with self.assertRaises(MermaidParseError) as raised_context:
            compile_body(
                "flowchart TD\n"
                f"  A001[任务a;;out=a.txt] -->|timeout: {overflow_digits}| "
                "B001[任务b;;out=b.txt]\n"
            )
        self.assertIn("not a finite", str(raised_context.exception))
        self.assertEqual(raised_context.exception.line_number, 8)


class BudgetTokensUpperBoundTest(unittest.TestCase):
    """budget_tokens must be at most MAX_BUDGET_TOKENS."""

    def test_budget_above_max_is_rejected(self):
        with self.assertRaises(MermaidParseError) as raised_context:
            compile_with_budget(
                MAX_BUDGET_TOKENS + 1, "flowchart TD\n  A001[任务a;;out=a.txt]\n"
            )
        self.assertIn("budget_tokens", str(raised_context.exception))
        self.assertIn("at most", str(raised_context.exception))
        self.assertEqual(raised_context.exception.line_number, 3)

    def test_budget_at_max_is_accepted(self):
        flow_ir = compile_with_budget(
            MAX_BUDGET_TOKENS, "flowchart TD\n  A001[任务a;;out=a.txt]\n"
        )
        self.assertEqual(flow_ir.budget_tokens, MAX_BUDGET_TOKENS)


class RetryUpperBoundTest(unittest.TestCase):
    """retry must be at most MAX_RETRY_ATTEMPTS."""

    def test_retry_above_max_is_rejected(self):
        with self.assertRaises(MermaidParseError) as raised_context:
            compile_body(
                "flowchart TD\n"
                f"  A001[任务a;;out=a.txt] -->|retry: {MAX_RETRY_ATTEMPTS + 1}| "
                "B001[任务b;;out=b.txt]\n"
            )
        self.assertIn("retry count", str(raised_context.exception))
        self.assertEqual(raised_context.exception.line_number, 8)

    def test_retry_at_max_is_accepted(self):
        flow_ir = compile_body(
            "flowchart TD\n"
            f"  A001[任务a;;out=a.txt] -->|retry: {MAX_RETRY_ATTEMPTS}| "
            "B001[任务b;;out=b.txt]\n"
        )
        self.assertEqual(flow_ir.edges[0].retry, MAX_RETRY_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
