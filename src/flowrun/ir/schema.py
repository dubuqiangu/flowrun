"""FlowRun intermediate representation.

Field names are frozen per flowrun/interfaces.md §2 — every other domain in the
project references these names, so they must not change without an
orchestrator decision.
"""

from __future__ import annotations

from dataclasses import dataclass

NODE_KIND_TASK = "task"
NODE_KIND_DECISION = "decision"
NODE_KIND_HUMAN = "human"
NODE_KIND_JOIN = "join"


@dataclass
class AgentCommandSpec:
    """Declarative command template for one agent CLI (interfaces.md §4)."""

    command: list[str]
    resume_mode: str | None = None

    def to_dict(self) -> dict:
        return {"command": list(self.command), "resume_mode": self.resume_mode}

    @classmethod
    def from_dict(cls, data: dict) -> "AgentCommandSpec":
        return cls(command=list(data["command"]), resume_mode=data.get("resume_mode"))


@dataclass
class NodeSpec:
    """One node of the compiled flow graph (including implicit join nodes)."""

    node_id: str
    kind: str  # NODE_KIND_TASK | NODE_KIND_DECISION | NODE_KIND_HUMAN | NODE_KIND_JOIN
    label: str
    agent: str | None
    outputs: list[str]
    line_number: int

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "label": self.label,
            "agent": self.agent,
            "outputs": list(self.outputs),
            "line_number": self.line_number,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "NodeSpec":
        return cls(
            node_id=data["node_id"],
            kind=data["kind"],
            label=data["label"],
            agent=data["agent"],
            outputs=list(data["outputs"]),
            line_number=data["line_number"],
        )


@dataclass
class EdgeSpec:
    """One directed edge with its execution attributes."""

    source_id: str
    target_id: str
    when: str | None  # None = unconditional; "all" = join barrier; other = natural-language predicate
    retry: int
    timeout_seconds: float | None
    line_number: int

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "when": self.when,
            "retry": self.retry,
            "timeout_seconds": self.timeout_seconds,
            "line_number": self.line_number,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EdgeSpec":
        return cls(
            source_id=data["source_id"],
            target_id=data["target_id"],
            when=data["when"],
            retry=data["retry"],
            timeout_seconds=data["timeout_seconds"],
            line_number=data["line_number"],
        )


@dataclass
class FlowIR:
    """Compiled, validated flow graph — the single source of truth for the kernel."""

    run_name: str
    default_agent: str
    budget_tokens: int
    agent_registry: dict[str, AgentCommandSpec]
    nodes: dict[str, NodeSpec]
    edges: list[EdgeSpec]
    entry_node_ids: list[str]

    def to_dict(self) -> dict:
        """JSON-serializable projection (golden-baseline format)."""
        return {
            "run_name": self.run_name,
            "default_agent": self.default_agent,
            "budget_tokens": self.budget_tokens,
            "agent_registry": {
                agent_name: spec.to_dict() for agent_name, spec in self.agent_registry.items()
            },
            "nodes": {node_id: spec.to_dict() for node_id, spec in self.nodes.items()},
            "edges": [edge.to_dict() for edge in self.edges],
            "entry_node_ids": list(self.entry_node_ids),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FlowIR":
        for required_key in (
            "run_name",
            "default_agent",
            "budget_tokens",
            "agent_registry",
            "nodes",
            "edges",
            "entry_node_ids",
        ):
            if required_key not in data:
                raise ValueError(f"FlowIR.from_dict: missing required key '{required_key}'")
        return cls(
            run_name=data["run_name"],
            default_agent=data["default_agent"],
            budget_tokens=data["budget_tokens"],
            agent_registry={
                agent_name: AgentCommandSpec.from_dict(spec)
                for agent_name, spec in data["agent_registry"].items()
            },
            nodes={node_id: NodeSpec.from_dict(spec) for node_id, spec in data["nodes"].items()},
            edges=[EdgeSpec.from_dict(edge) for edge in data["edges"]],
            entry_node_ids=list(data["entry_node_ids"]),
        )
