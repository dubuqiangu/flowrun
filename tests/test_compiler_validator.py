"""Tests for flowrun.compiler.validator (tasks.md T003) including the golden
JSON baseline of the sample flow (tasks.md T001 acceptance)."""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from flowrun.compiler.mermaid_parser import parse_flowchart
from flowrun.compiler.validator import (
    FlowValidationError,
    collect_validation_errors,
    compile_flow_source,
)
from flowrun.ir.schema import FlowIR

EXAMPLE_FLOW_PATH = PROJECT_ROOT / "examples" / "flow.mmd"

# Golden baseline: the compiled product of examples/flow.mmd
# (design.md §4.1 sample, verbatim). Line numbers refer to flow.mmd.
EXPECTED_GOLDEN_FLOW_IR = {
    "run_name": "release-login",
    "default_agent": "claude-code",
    "budget_tokens": 1000000,
    "agent_registry": {
        "claude-code": {
            "command": ["claude", "-p", "--output-format", "stream-json"],
            "resume_mode": "session",
        },
        "opencode": {
            "command": [
                "opencode",
                "run",
                "--format",
                "json",
                "--file",
                "__FLOW_PACKET_PATH__",
            ],
            "resume_mode": None,
        },
    },
    "nodes": {
        "T001": {
            "node_id": "T001",
            "kind": "task",
            "label": "实现登录API",
            "agent": "claude-code",
            "outputs": ["src/auth.py", "test/auth.spec.ts"],
            "line_number": 9,
        },
        "T002": {
            "node_id": "T002",
            "kind": "task",
            "label": "实现登录UI",
            "agent": "opencode",
            "outputs": ["src/ui/Login.tsx"],
            "line_number": 10,
        },
        "J001": {
            "node_id": "J001",
            "kind": "join",
            "label": "集成准备",
            "agent": None,
            "outputs": [],
            "line_number": 11,
        },
        "D001": {
            "node_id": "D001",
            "kind": "decision",
            "label": "验收通过?",
            "agent": None,
            "outputs": [],
            "line_number": 11,
        },
        "H001": {
            "node_id": "H001",
            "kind": "human",
            "label": "发布人审",
            "agent": None,
            "outputs": [],
            "line_number": 12,
        },
        "T004": {
            "node_id": "T004",
            "kind": "task",
            "label": "打 tag 发布",
            "agent": None,
            "outputs": [],
            "line_number": 13,
        },
        "T003": {
            "node_id": "T003",
            "kind": "task",
            "label": "失败诊断",
            "agent": "claude-code",
            "outputs": [],
            "line_number": 14,
        },
        "T005": {
            "node_id": "T005",
            "kind": "task",
            "label": "超时诊断",
            "agent": "claude-code",
            "outputs": [],
            "line_number": 15,
        },
    },
    "edges": [
        {"source_id": "T001", "target_id": "J001", "when": None, "retry": 0,
         "timeout_seconds": None, "line_number": 9},
        {"source_id": "T002", "target_id": "J001", "when": None, "retry": 0,
         "timeout_seconds": None, "line_number": 10},
        {"source_id": "J001", "target_id": "D001", "when": "all", "retry": 0,
         "timeout_seconds": None, "line_number": 11},
        {"source_id": "D001", "target_id": "H001", "when": "ok", "retry": 0,
         "timeout_seconds": None, "line_number": 12},
        {"source_id": "H001", "target_id": "T004", "when": None, "retry": 0,
         "timeout_seconds": None, "line_number": 13},
        {"source_id": "D001", "target_id": "T003", "when": "fail", "retry": 0,
         "timeout_seconds": None, "line_number": 14},
        {"source_id": "D001", "target_id": "T005", "when": None, "retry": 0,
         "timeout_seconds": 1800.0, "line_number": 15},
    ],
    "entry_node_ids": ["T001", "T002"],
}

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


class GoldenBaselineTest(unittest.TestCase):
    def test_sample_flow_compiles_to_golden_ir_with_zero_errors(self):
        flow_source = EXAMPLE_FLOW_PATH.read_text(encoding="utf-8")
        self.assertEqual(collect_validation_errors(parse_flowchart(flow_source)), [])
        compiled_ir = compile_flow_source(flow_source)
        self.assertEqual(compiled_ir.to_dict(), EXPECTED_GOLDEN_FLOW_IR)

    def test_golden_dict_roundtrips_through_from_dict(self):
        reconstructed_ir = FlowIR.from_dict(EXPECTED_GOLDEN_FLOW_IR)
        self.assertEqual(reconstructed_ir.to_dict(), EXPECTED_GOLDEN_FLOW_IR)
        self.assertEqual(reconstructed_ir.nodes["J001"].kind, "join")


class JoinMarkingTest(unittest.TestCase):
    def test_multi_in_node_with_incoming_when_all_becomes_join(self):
        flow_ir = compile_body(
            "flowchart TD\n"
            "  A001[任务a;;out=a.txt] -->|when: all| J001[汇合点]\n"
            "  B001[任务b;;out=b.txt] -->|when: all| J001\n"
            "  J001 --> C001[任务c;;out=c.txt]\n"
        )
        self.assertEqual(flow_ir.nodes["J001"].kind, "join")

    def test_multi_in_node_with_outgoing_when_all_becomes_join(self):
        # The design.md §4.1 sample form: the barrier sits on the join's out-edge.
        flow_ir = compile_body(
            "flowchart TD\n"
            "  A001[任务a;;out=a.txt] --> J001[汇合点]\n"
            "  B001[任务b;;out=b.txt] --> J001\n"
            "  J001 -->|when: all| D001{继续?}\n"
            "  D001 -->|when: ok| C001[任务c;;out=c.txt]\n"
        )
        self.assertEqual(flow_ir.nodes["J001"].kind, "join")

    def test_decision_and_human_nodes_keep_their_kind_with_multiple_incoming(self):
        flow_ir = compile_body(
            "flowchart TD\n"
            "  A001[任务a;;out=a.txt] -->|when: all| H001([人审节点])\n"
            "  B001[任务b;;out=b.txt] -->|when: all| H001\n"
        )
        self.assertEqual(flow_ir.nodes["H001"].kind, "human")


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


class BadGraphTest(ValidationErrorAssertions, unittest.TestCase):
    def test_unreachable_nodes_are_reported(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=a.txt] --> B001[任务b;;out=b.txt]\n"
            "  X001[孤岛一;;out=x.txt] --> Y001[孤岛二;;out=y.txt]\n"
            "  Y001 --> X001\n"
        )
        self.assert_single_error(errors, 9, "'X001' is unreachable")
        self.assert_single_error(errors, 9, "'Y001' is unreachable")

    def test_multi_in_node_without_barrier_is_reported(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=a.txt] --> J001[汇合点]\n"
            "  B001[任务b;;out=b.txt] --> J001\n"
        )
        self.assert_single_error(
            errors, 8, "multiple incoming edges but no 'when: all' barrier"
        )
        self.assertEqual(len(errors), 1)

    def test_task_with_empty_declared_out_contract_is_reported(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=] --> B001[任务b;;out=b.txt]\n"
        )
        self.assert_single_error(errors, 8, "'out=' with an empty artifact contract")
        self.assertEqual(len(errors), 1)

    def test_unregistered_agent_reference_is_reported(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;agent=ghost;;out=a.txt] --> B001[任务b;;out=b.txt]\n"
        )
        self.assert_single_error(
            errors, 8, "references agent 'ghost' which is not registered"
        )
        self.assertEqual(len(errors), 1)

    def test_decision_without_conditional_out_edge_is_reported(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  D001{验收?} --> A001[任务a;;out=a.txt]\n"
        )
        self.assert_single_error(
            errors, 8, "needs at least one outgoing edge labelled with a non-'all' 'when:'"
        )
        self.assertEqual(len(errors), 1)

    def test_missing_entry_node_is_reported(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=a.txt] --> B001[任务b;;out=b.txt]\n"
            "  B001 --> A001\n"
        )
        self.assert_single_error(errors, 7, "no entry node")
        self.assertEqual(len(errors), 1)

    def test_edge_to_undefined_node_is_reported(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=a.txt] --> GHOST\n"
        )
        self.assert_single_error(errors, 8, "undefined node 'GHOST'")
        self.assertEqual(len(errors), 1)

    def test_timeout_edge_to_undefined_node_is_reported(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=a.txt] --> D001{继续?}\n"
            "  D001 -->|when: ok| B001[任务b;;out=b.txt]\n"
            "  D001 -->|timeout: 30m| VANISHED\n"
        )
        self.assert_single_error(errors, 10, "undefined node 'VANISHED'")
        self.assertEqual(len(errors), 1)

    def test_undefined_standalone_reference_is_reported(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=a.txt] --> B001[任务b;;out=b.txt]\n"
            "  Z999\n"
        )
        self.assert_single_error(errors, 9, "'Z999' is never defined")

    def test_default_agent_not_in_registry_is_reported(self):
        source_text = (
            "---\n"
            "run: test-run\n"
            "defaults: {agent: ghost, budget_tokens: 5000}\n"
            "agents:\n"
            "  tester: {cmd: \"fake-agent run\"}\n"
            "---\n"
            "flowchart TD\n"
            "  A001[任务a;;out=a.txt]\n"
        )
        errors = collect_validation_errors(parse_flowchart(source_text))
        self.assert_single_error(errors, 1, "default agent 'ghost' is not registered")

    def test_missing_run_in_frontmatter_is_reported(self):
        source_text = (
            "---\n"
            "defaults: {agent: tester, budget_tokens: 5000}\n"
            "agents:\n"
            "  tester: {cmd: \"fake-agent run\"}\n"
            "---\n"
            "flowchart TD\n"
            "  A001[任务a;;out=a.txt]\n"
        )
        errors = collect_validation_errors(parse_flowchart(source_text))
        self.assert_single_error(errors, 1, "must declare 'run:")

    def test_build_flow_ir_raises_with_error_list(self):
        with self.assertRaises(FlowValidationError) as raised_context:
            compile_body(
                "flowchart TD\n"
                "  A001[任务a;;out=a.txt] --> GHOST\n"
            )
        validation_error = raised_context.exception
        self.assertEqual(len(validation_error.errors), 1)
        self.assertEqual(validation_error.errors[0].line_number, 8)
        self.assertIn("undefined node 'GHOST'", str(validation_error))


class OutputPathEscapeTest(ValidationErrorAssertions, unittest.TestCase):
    """FIX-4：out= 必须是 workspace 内的相对路径（防存在性 oracle / 契约逃逸）。"""

    def test_parent_directory_traversal_is_rejected(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=../../outside.txt] --> B001[任务b;;out=b.txt]\n"
        )
        self.assert_single_error(errors, 8, "'..' path segments")
        self.assertEqual(len(errors), 1)

    def test_windows_drive_absolute_path_is_rejected(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=C:\\Windows\\win.ini] --> B001[任务b;;out=b.txt]\n"
        )
        self.assert_single_error(errors, 8, "must be a relative path")
        self.assertEqual(len(errors), 1)

    def test_windows_drive_relative_path_is_rejected(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=D:escape.txt] --> B001[任务b;;out=b.txt]\n"
        )
        self.assert_single_error(errors, 8, "must be a relative path")
        self.assertEqual(len(errors), 1)

    def test_posix_absolute_path_is_rejected(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=/etc/passwd] --> B001[任务b;;out=b.txt]\n"
        )
        self.assert_single_error(errors, 8, "must be a relative path")
        self.assertEqual(len(errors), 1)

    def test_windows_root_relative_path_is_rejected(self):
        # C1 回归：\Windows\win.ini 没有盘符、is_absolute() 也是 False，但
        # workspace / out 会解析到盘根 C:\Windows\win.ini —— 契约整个跑到
        # workspace 外边，缺件存在性门形同虚设。
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=\\Windows\\win.ini] --> B001[任务b;;out=b.txt]\n"
        )
        self.assert_single_error(errors, 8, "must be a relative path")
        self.assertEqual(len(errors), 1)

    def test_single_segment_root_relative_paths_are_rejected(self):
        for root_relative_path in ("\\escape.txt", "/escape.txt"):
            with self.subTest(root_relative_path=root_relative_path):
                errors = collect_errors_for(
                    "flowchart TD\n"
                    f"  A001[任务a;;out={root_relative_path}] --> "
                    "B001[任务b;;out=b.txt]\n"
                )
                self.assert_single_error(errors, 8, "must be a relative path")
                self.assertEqual(len(errors), 1)

    def test_unc_share_path_is_still_rejected(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=\\\\server\\share\\x.txt] --> B001[任务b;;out=b.txt]\n"
        )
        self.assert_single_error(errors, 8, "must be a relative path")
        self.assertEqual(len(errors), 1)

    def test_workspace_relative_forms_are_all_accepted(self):
        flow_ir = compile_body(
            "flowchart TD\n"
            "  A001[任务a;;out=out/a.txt,a\\b.txt,./c.txt] --> "
            "B001[任务b;;out=docs/d.md]\n"
        )
        self.assertEqual(
            flow_ir.nodes["A001"].outputs, ["out/a.txt", "a\\b.txt", "./c.txt"]
        )
        self.assertEqual(flow_ir.nodes["B001"].outputs, ["docs/d.md"])

    def test_nested_relative_paths_are_kept(self):
        flow_ir = compile_body(
            "flowchart TD\n"
            "  A001[任务a;;out=src/auth.py,test/auth.spec.ts] --> B001[任务b;;out=docs/a.md]\n"
        )
        self.assertEqual(
            flow_ir.nodes["A001"].outputs, ["src/auth.py", "test/auth.spec.ts"]
        )
        self.assertEqual(flow_ir.nodes["B001"].outputs, ["docs/a.md"])

    def test_second_bad_output_in_a_list_is_reported(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=ok.txt,../escape.txt] --> B001[任务b;;out=b.txt]\n"
        )
        self.assert_single_error(errors, 8, "'../escape.txt'")
        self.assertEqual(len(errors), 1)


class WhenBarrierCaseInGraphTest(ValidationErrorAssertions, unittest.TestCase):
    """FIX-8（图侧）：大小写变体的 when: all 必须被当成 barrier。"""

    def test_uppercase_when_all_marks_join(self):
        flow_ir = compile_body(
            "flowchart TD\n"
            "  A001[任务a;;out=a.txt] -->|when: ALL| J001[汇合点]\n"
            "  B001[任务b;;out=b.txt] -->|when: All| J001\n"
        )
        self.assertEqual(flow_ir.nodes["J001"].kind, "join")
        self.assertEqual(
            sorted(edge.when for edge in flow_ir.edges), ["all", "all"]
        )

    def test_uppercase_when_all_is_not_treated_as_natural_language_predicate(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  D001{验收?} -->|when: ALL| A001[任务a;;out=a.txt]\n"
        )
        self.assert_single_error(
            errors, 8, "needs at least one outgoing edge labelled with a non-'all'"
        )
        self.assertEqual(len(errors), 1)


class TimeoutEdgeConstraintTest(ValidationErrorAssertions, unittest.TestCase):
    """FIX-10：timeout 边的组合约束。"""

    def test_multiple_timeout_edges_from_same_source_are_rejected(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=a.txt] --> B001[任务b;;out=b.txt]\n"
            "  A001 -->|timeout: 5m| C001[任务c;;out=c.txt]\n"
            "  A001 -->|timeout: 6m| D001[任务d;;out=d.txt]\n"
        )
        self.assert_single_error(
            errors, 10, "at most one 'timeout:' edge per source node"
        )
        self.assertEqual(len(errors), 1)

    def test_timeout_edge_to_join_is_rejected(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=a.txt] -->|when: all| J001[汇合点]\n"
            "  B001[任务b;;out=b.txt] -->|when: all| J001\n"
            "  A001 -->|timeout: 5m| J001\n"
        )
        self.assert_single_error(errors, 10, "must not target a 'join' node")
        self.assertEqual(len(errors), 1)

    def test_timeout_edge_to_decision_is_rejected(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=a.txt] --> B001[任务b;;out=b.txt]\n"
            "  A001 -->|timeout: 5m| D001{继续?}\n"
            "  D001 -->|when: ok| E001[任务e;;out=e.txt]\n"
        )
        self.assert_single_error(errors, 9, "must not target a 'decision' node")
        self.assertEqual(len(errors), 1)

    def test_timeout_edge_with_retry_is_rejected(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=a.txt] --> B001[任务b;;out=b.txt]\n"
            "  A001 -->|timeout: 5m, retry: 3| C001[任务c;;out=c.txt]\n"
        )
        self.assert_single_error(errors, 9, "must not be combined with 'retry:")
        self.assertEqual(len(errors), 1)

    def test_legal_timeout_edge_still_compiles(self):
        flow_ir = compile_body(
            "flowchart TD\n"
            "  A001[任务a;;out=a.txt] --> D001{继续?}\n"
            "  D001 -->|when: ok| B001[任务b;;out=b.txt]\n"
            "  D001 -->|timeout: 30m| C001[超时诊断;;out=c.txt]\n"
        )
        timeout_edges = [
            edge for edge in flow_ir.edges
            if edge.timeout_seconds is not None and edge.when is None
        ]
        self.assertEqual(len(timeout_edges), 1)
        self.assertEqual(timeout_edges[0].target_id, "C001")


class SelfLoopEdgeTest(ValidationErrorAssertions, unittest.TestCase):
    """FIX-11：自环边直接编译错。"""

    def test_self_loop_edge_is_rejected(self):
        errors = collect_errors_for(
            "flowchart TD\n  A001[任务a;;out=a.txt] --> A001\n"
        )
        self.assert_single_error(errors, 8, "self-loop")
        self.assertEqual(len(errors), 1)

    def test_self_loop_between_other_nodes_does_not_mask_the_error(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=a.txt] --> B001[任务b;;out=b.txt]\n"
            "  B001 --> B001\n"
        )
        self.assert_single_error(errors, 9, "self-loop")


if __name__ == "__main__":
    unittest.main()
