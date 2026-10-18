"""Tests for two compiler rules kept out of the (already long) validator suite:

- 'out=' values must not contain ':' (an NTFS alternate data stream such as
  'a.txt:stream' names a hidden stream on the same directory entry and is
  neither absolute nor drive-qualified, so it used to slip through);
- a 'timeout:' edge must not target a 'human' node, alongside the existing
  'join' / 'decision' rejections.
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from flowrun.compiler.mermaid_parser import parse_flowchart
from flowrun.compiler.validator import collect_validation_errors, compile_flow_source

TEST_FRONTMATTER = """---
run: test-run
defaults: {agent: tester, budget_tokens: 5000}
agents:
  tester: {cmd: "fake-agent run"}
---
"""


def compile_body(body_text: str):
    return compile_flow_source(TEST_FRONTMATTER + body_text)


def collect_errors_for(body_text: str):
    return collect_validation_errors(parse_flowchart(TEST_FRONTMATTER + body_text))


class PathAssertions:
    """按消息片段定位一条编译错并核对行号。"""

    def assert_single_error(self, errors, expected_line_number, message_fragment):
        matching_errors = [
            validation_error
            for validation_error in errors
            if message_fragment in validation_error.message
        ]
        self.assertTrue(
            matching_errors,
            f"expected an error containing '{message_fragment}', got: {errors}",
        )
        self.assertEqual(matching_errors[0].line_number, expected_line_number)


class OutputAlternateDataStreamTest(PathAssertions, unittest.TestCase):
    """FIX: 'out=' must reject NTFS alternate data stream syntax."""

    def test_alternate_data_stream_is_rejected(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=a.txt:stream] --> B001[任务b;;out=b.txt]\n"
        )
        self.assert_single_error(errors, 8, "must not contain ':'")
        self.assertEqual(len(errors), 1)

    def test_alternate_data_stream_in_second_path_is_rejected(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=ok.txt,report.txt:hidden] --> B001[任务b;;out=b.txt]\n"
        )
        self.assert_single_error(errors, 8, "report.txt:hidden")
        self.assertEqual(len(errors), 1)

    def test_plain_relative_out_still_compiles(self):
        flow_ir = compile_body(
            "flowchart TD\n"
            "  A001[任务a;;out=src/auth.py] --> B001[任务b;;out=docs/d.md]\n"
        )
        self.assertEqual(flow_ir.nodes["A001"].outputs, ["src/auth.py"])

    def test_drive_qualified_path_keeps_its_absolute_message(self):
        # The ':' check must sit after the drive/absolute check, so a
        # drive-qualified path is still reported as an absolute escape.
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=D:escape.txt] --> B001[任务b;;out=b.txt]\n"
        )
        self.assert_single_error(errors, 8, "must be a relative path")


class TimeoutEdgeToHumanTest(PathAssertions, unittest.TestCase):
    """FIX: 'timeout:' edges must not target a 'human' node (join/decision peer)."""

    def test_timeout_edge_to_human_is_rejected(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=a.txt] --> D001{继续?}\n"
            "  D001 -->|when: ok| B001[任务b;;out=b.txt]\n"
            "  D001 -->|timeout: 5m| H001([人审节点])\n"
        )
        self.assert_single_error(errors, 10, "must not target a 'human' node")
        self.assertEqual(len(errors), 1)

    def test_timeout_edge_to_task_still_compiles(self):
        flow_ir = compile_body(
            "flowchart TD\n"
            "  A001[任务a;;out=a.txt] -->|timeout: 5m| B001[任务b;;out=b.txt]\n"
        )
        self.assertEqual(flow_ir.edges[0].timeout_seconds, 300.0)


if __name__ == "__main__":
    unittest.main()
