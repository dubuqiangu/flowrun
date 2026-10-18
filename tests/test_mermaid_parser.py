"""Tests for flowrun.compiler.mermaid_parser (tasks.md T002): node/edge parsing,
line classification, subgraph structure and the case-insensitive 'when: all' barrier."""

import sys
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import mermaid_parser_support

from mermaid_parser_support import parse_body  # noqa: E402  (负责把 src/ 加进 sys.path)

from flowrun.compiler.mermaid_parser import (  # noqa: E402
    MermaidParseError,
    parse_flowchart,
)


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


if __name__ == "__main__":
    unittest.main()
