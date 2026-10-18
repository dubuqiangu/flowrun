"""compiler.validator 测试共享支撑：路径引导、坏图 frontmatter、编译/校验入口、错误断言混入。

被 test_compiler_*.py 复用（文件名不带 test_ 前缀，避免被 unittest 收集）。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from flowrun.compiler.mermaid_parser import parse_flowchart  # noqa: E402
from flowrun.compiler.validator import (  # noqa: E402
    collect_validation_errors,
    compile_flow_source,
)

# Minimal valid frontmatter for bad-graph fixtures (lines 1-6; body from line 7).
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


class ValidationErrorAssertions:
    """共享断言：按消息片段定位一条编译错并核对行号。"""

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
