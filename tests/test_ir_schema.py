"""Tests for flowrun.ir.schema (tasks.md T001): frozen IR dataclass shapes."""

import sys
import unittest
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from flowrun.ir.schema import AgentCommandSpec, EdgeSpec, FlowIR, NodeSpec


def build_reference_flow_ir() -> FlowIR:
    """A small but complete FlowIR covering every field type."""
    return FlowIR(
        run_name="release-login",
        default_agent="claude-code",
        budget_tokens=1000000,
        agent_registry={
            "claude-code": AgentCommandSpec(
                command=["claude", "-p", "--output-format", "stream-json"],
                resume_mode="session",
            ),
            "opencode": AgentCommandSpec(command=["opencode", "run"], resume_mode=None),
        },
        nodes={
            "T001": NodeSpec(
                node_id="T001",
                kind="task",
                label="实现登录API",
                agent="claude-code",
                outputs=["src/auth.py", "test/auth.spec.ts"],
                line_number=9,
            ),
            "J001": NodeSpec(
                node_id="J001",
                kind="join",
                label="集成准备",
                agent=None,
                outputs=[],
                line_number=11,
            ),
        },
        edges=[
            EdgeSpec(
                source_id="T001",
                target_id="J001",
                when=None,
                retry=0,
                timeout_seconds=None,
                line_number=9,
            ),
            EdgeSpec(
                source_id="J001",
                target_id="D001",
                when="all",
                retry=2,
                timeout_seconds=1800.0,
                line_number=11,
            ),
        ],
        entry_node_ids=["T001"],
    )


class AgentCommandSpecTest(unittest.TestCase):
    def test_roundtrip_through_dict(self):
        command_spec = AgentCommandSpec(
            command=["opencode", "run", "--format", "json"], resume_mode="session"
        )
        self.assertEqual(AgentCommandSpec.from_dict(command_spec.to_dict()), command_spec)

    def test_resume_mode_defaults_to_none(self):
        command_spec = AgentCommandSpec(command=["fake-agent"])
        self.assertIsNone(command_spec.resume_mode)
        self.assertEqual(command_spec.to_dict()["resume_mode"], None)


class NodeSpecEdgeSpecFieldTest(unittest.TestCase):
    def test_node_spec_holds_all_frozen_fields(self):
        node_spec = NodeSpec(
            node_id="H001", kind="human", label="发布人审",
            agent=None, outputs=[], line_number=12,
        )
        self.assertEqual(node_spec.node_id, "H001")
        self.assertEqual(node_spec.kind, "human")
        self.assertEqual(node_spec.label, "发布人审")
        self.assertIsNone(node_spec.agent)
        self.assertEqual(node_spec.outputs, [])
        self.assertEqual(node_spec.line_number, 12)

    def test_edge_spec_holds_all_frozen_fields(self):
        edge_spec = EdgeSpec(
            source_id="D001", target_id="T001", when="fail",
            retry=2, timeout_seconds=None, line_number=13,
        )
        self.assertEqual(edge_spec.source_id, "D001")
        self.assertEqual(edge_spec.target_id, "T001")
        self.assertEqual(edge_spec.when, "fail")
        self.assertEqual(edge_spec.retry, 2)
        self.assertIsNone(edge_spec.timeout_seconds)
        self.assertEqual(edge_spec.line_number, 13)


class FlowIRSerializationTest(unittest.TestCase):
    def test_to_dict_is_json_serializable_and_matches_frozen_shape(self):
        import json

        projected_dict = build_reference_flow_ir().to_dict()
        # JSON round-trip proves the projection is serializable (golden baseline).
        json.loads(json.dumps(projected_dict, ensure_ascii=False))
        self.assertEqual(
            sorted(projected_dict.keys()),
            sorted(
                [
                    "run_name", "default_agent", "budget_tokens",
                    "agent_registry", "nodes", "edges", "entry_node_ids",
                ]
            ),
        )
        self.assertEqual(
            sorted(projected_dict["nodes"]["T001"].keys()),
            sorted(["node_id", "kind", "label", "agent", "outputs", "line_number"]),
        )
        self.assertEqual(
            sorted(projected_dict["edges"][0].keys()),
            sorted(
                ["source_id", "target_id", "when", "retry", "timeout_seconds", "line_number"]
            ),
        )

    def test_from_dict_reconstructs_equivalent_ir(self):
        flow_ir = build_reference_flow_ir()
        reconstructed_ir = FlowIR.from_dict(flow_ir.to_dict())
        self.assertEqual(reconstructed_ir, flow_ir)
        self.assertEqual(reconstructed_ir.to_dict(), flow_ir.to_dict())

    def test_from_dict_missing_key_raises_value_error(self):
        with self.assertRaises(ValueError) as raised_context:
            FlowIR.from_dict({"run_name": "release-login"})
        self.assertIn("missing required key", str(raised_context.exception))


if __name__ == "__main__":
    unittest.main()
