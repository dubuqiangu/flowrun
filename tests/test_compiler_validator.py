"""Tests for flowrun.compiler.validator (tasks.md T003): join marking and the
bad-graph validation error set."""

import sys
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import compiler_validator_support

from compiler_validator_support import (  # noqa: E402  (负责把 src/ 加进 sys.path)
    ValidationErrorAssertions,
    collect_errors_for,
    compile_body,
)

from flowrun.compiler.mermaid_parser import parse_flowchart  # noqa: E402
from flowrun.compiler.validator import (  # noqa: E402
    FlowValidationError,
    collect_validation_errors,
)


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


if __name__ == "__main__":
    unittest.main()
