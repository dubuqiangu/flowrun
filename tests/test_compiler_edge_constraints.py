"""图侧边约束测试：大小写变体的 when: all barrier（FIX-8）、timeout 边组合约束
（FIX-10）、自环边编译错（FIX-11）。"""

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
