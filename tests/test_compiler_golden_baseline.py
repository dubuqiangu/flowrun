"""Golden JSON baseline of the sample flow (tasks.md T001 acceptance):
the compiled product of examples/flow.mmd (design.md §4.1 sample, verbatim)."""

import sys
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import compiler_validator_support

from compiler_validator_support import PROJECT_ROOT  # noqa: E402  (负责把 src/ 加进 sys.path)

from flowrun.compiler.mermaid_parser import parse_flowchart  # noqa: E402
from flowrun.compiler.validator import (  # noqa: E402
    collect_validation_errors,
    compile_flow_source,
)
from flowrun.ir.schema import FlowIR  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
