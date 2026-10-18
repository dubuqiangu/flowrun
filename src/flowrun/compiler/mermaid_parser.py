"""Document-level parser for flow.mmd: frontmatter + Mermaid flowchart subset.

Grammar accepted (design.md §4.1, interfaces.md domain A):
- optional YAML frontmatter (parsed by flowrun.compiler.frontmatter)
- a ``flowchart TD`` / ``flowchart LR`` declaration line
- node definitions: ``T001[label;;agent=x;;out=a,b]``, ``D001{label}``,
  ``H001([label])``; bare ids act as references
- edges: ``-->``, ``--->``, ``---``, each with an optional
  ``|when: ..., retry: N, timeout: 30m|`` label
- ``subgraph`` / ``end`` / ``direction`` lines are ignored (M1: comment-only
  grouping, they never enter the IR)
- ``%%`` comments (full-line and trailing) are ignored

sequenceDiagram sources are rejected with an explicit error (no executable
semantics, design.md §4.5). Line-level syntax lives in
flowrun.compiler.mermaid_syntax; this module assembles lines into a
FlowchartDocument and extracts the frontmatter flow metadata.
"""

from __future__ import annotations

from dataclasses import dataclass

from flowrun.compiler.agent_command_splitter import split_agent_command
from flowrun.compiler.frontmatter import (
    FrontmatterResult,
    parse_frontmatter_document,
    separate_frontmatter,
)
from flowrun.compiler.limits import MAX_BUDGET_TOKENS
from flowrun.compiler.mermaid_syntax import (
    MermaidParseError,
    ParsedEdge,
    ParsedNode,
    parse_content_line,
)
from flowrun.ir.schema import AgentCommandSpec

__all__ = [
    "FlowchartDocument",
    "MermaidParseError",
    "ParsedEdge",
    "ParsedNode",
    "parse_flowchart",
    "split_agent_command",
]


@dataclass
class FlowchartDocument:
    """Raw parse tree: frontmatter metadata plus nodes/edges with source line numbers."""

    run_name: str | None
    default_agent: str | None
    budget_tokens: int | None
    agent_registry: dict[str, AgentCommandSpec]
    frontmatter_line_number: int | None
    declaration_line_number: int | None
    nodes: dict[str, ParsedNode]
    edges: list[ParsedEdge]
    bare_references: dict[str, int]  # node id -> line of a standalone bare reference


def parse_flowchart(source_text: str) -> FlowchartDocument:
    """Parse flow.mmd source text into a FlowchartDocument (no graph validation)."""
    if source_text.startswith("\ufeff"):
        source_text = source_text.lstrip("\ufeff")
    frontmatter_lines, body_lines, had_frontmatter = separate_frontmatter(source_text)
    if had_frontmatter:
        frontmatter_result = parse_frontmatter_document(frontmatter_lines)
        run_name, default_agent, budget_tokens, agent_registry = _extract_flow_metadata(
            frontmatter_result
        )
        frontmatter_line_number = 1
    else:
        run_name, default_agent, budget_tokens, agent_registry = None, None, None, {}
        frontmatter_line_number = None

    nodes: dict[str, ParsedNode] = {}
    edges: list[ParsedEdge] = []
    bare_references: dict[str, int] = {}
    declaration_line_number: int | None = None
    # subgraph 起始行栈：EOF 时栈非空 = 有 subgraph 缺 'end'（M1: 注释性分组，
    # 但结构必须闭合，否则分组边界会静默吞掉后续行）。
    open_subgraph_lines: list[int] = []
    for line_number, line_text in body_lines:
        stripped_text = line_text.strip()
        if not stripped_text or stripped_text.startswith("%%"):
            continue
        first_word = stripped_text.split(None, 1)[0]
        first_word_lower = first_word.lower()
        if first_word_lower == "subgraph":
            open_subgraph_lines.append(line_number)
            continue
        if first_word_lower == "end" and stripped_text.lower() == "end":
            if not open_subgraph_lines:
                raise MermaidParseError("'end' without a matching 'subgraph'", line_number)
            open_subgraph_lines.pop()
            continue
        if first_word_lower == "direction":
            continue
        if first_word_lower == "sequencediagram":
            raise MermaidParseError(
                "sequenceDiagram has no executable semantics in FlowRun (design.md §4.5); "
                "only 'flowchart TD' / 'flowchart LR' sources are supported",
                line_number,
            )
        if first_word_lower == "flowchart":
            if declaration_line_number is not None:
                raise MermaidParseError(
                    f"duplicate 'flowchart' declaration (first declared at line "
                    f"{declaration_line_number})",
                    line_number,
                )
            declaration_line_number = _parse_declaration_line(stripped_text, line_number)
            continue
        if declaration_line_number is None:
            raise MermaidParseError(
                f"expected 'flowchart TD' or 'flowchart LR' as the first line, "
                f"got '{stripped_text[:30]}'",
                line_number,
            )
        parse_content_line(stripped_text, line_number, nodes, edges, bare_references)

    if open_subgraph_lines:
        opening_line_number = open_subgraph_lines[-1]
        raise MermaidParseError(
            f"subgraph opened at line {opening_line_number} is never closed with 'end'",
            opening_line_number,
        )
    if declaration_line_number is None:
        raise MermaidParseError(
            "missing 'flowchart TD' / 'flowchart LR' declaration line",
            frontmatter_line_number or 1,
        )
    return FlowchartDocument(
        run_name=run_name,
        default_agent=default_agent,
        budget_tokens=budget_tokens,
        agent_registry=agent_registry,
        frontmatter_line_number=frontmatter_line_number,
        declaration_line_number=declaration_line_number,
        nodes=nodes,
        edges=edges,
        bare_references=bare_references,
    )


def _parse_declaration_line(stripped_text: str, line_number: int) -> int:
    declaration_words = stripped_text.split()
    if len(declaration_words) != 2 or declaration_words[1].upper() not in ("TD", "LR"):
        raise MermaidParseError(
            f"invalid flowchart declaration '{stripped_text}': "
            "expected 'flowchart TD' or 'flowchart LR'",
            line_number,
        )
    return line_number


def _extract_flow_metadata(
    frontmatter_result: FrontmatterResult,
) -> tuple[str | None, str | None, int | None, dict[str, AgentCommandSpec]]:
    """Map the parsed frontmatter dict onto run-level metadata and the agent registry."""
    document = frontmatter_result.document
    key_lines = frontmatter_result.key_line_numbers
    run_name = _optional_string_value(document, "run", key_lines)
    default_agent: str | None = None
    budget_tokens: int | None = None
    defaults_section = document.get("defaults")
    if defaults_section is not None:
        if not isinstance(defaults_section, dict):
            raise MermaidParseError(
                "frontmatter 'defaults' must be an inline mapping like "
                "'defaults: {agent: ..., budget_tokens: ...}'",
                key_lines.get("defaults", 1),
            )
        default_agent = _optional_string_value(
            defaults_section, "agent", key_lines, prefix="defaults"
        )
        budget_entry = defaults_section.get("budget_tokens")
        if budget_entry is not None:
            if not isinstance(budget_entry, int) or isinstance(budget_entry, bool):
                raise MermaidParseError(
                    "frontmatter 'defaults.budget_tokens' must be an integer",
                    key_lines.get("defaults", 1),
                )
            if budget_entry <= 0:
                raise MermaidParseError(
                    "frontmatter 'defaults.budget_tokens' must be greater than 0 "
                    f"(got {budget_entry}); a non-positive budget trips the token gate "
                    "on the very first hop",
                    key_lines.get("defaults", 1),
                )
            if budget_entry > MAX_BUDGET_TOKENS:
                raise MermaidParseError(
                    "frontmatter 'defaults.budget_tokens' must be at most "
                    f"{MAX_BUDGET_TOKENS} tokens (got {budget_entry})",
                    key_lines.get("defaults", 1),
                )
            budget_tokens = budget_entry
    agent_registry: dict[str, AgentCommandSpec] = {}
    agents_section = document.get("agents")
    if agents_section is not None:
        if not isinstance(agents_section, dict):
            raise MermaidParseError(
                "frontmatter 'agents' must be a mapping of agent name -> {cmd: ...}",
                key_lines.get("agents", 1),
            )
        for agent_name, agent_entry in agents_section.items():
            entry_line = key_lines.get(f"agents.{agent_name}", key_lines.get("agents", 1))
            if not isinstance(agent_entry, dict):
                raise MermaidParseError(
                    f"frontmatter agent '{agent_name}' must be an inline mapping like "
                    "{cmd: \"...\"}",
                    entry_line,
                )
            command_text = agent_entry.get("cmd")
            if not isinstance(command_text, str) or not command_text.strip():
                raise MermaidParseError(
                    f"frontmatter agent '{agent_name}' must declare a 'cmd: \"...\"' string",
                    entry_line,
                )
            try:
                command_words = split_agent_command(command_text)
            except ValueError as shlex_error:
                raise MermaidParseError(
                    f"frontmatter agent '{agent_name}' has an unparsable cmd: {shlex_error}",
                    entry_line,
                ) from shlex_error
            if not command_words:
                raise MermaidParseError(
                    f"frontmatter agent '{agent_name}' has an empty cmd", entry_line
                )
            resume_value = agent_entry.get("resume")
            if resume_value is not None and not isinstance(resume_value, str):
                raise MermaidParseError(
                    f"frontmatter agent '{agent_name}' resume must be a string", entry_line
                )
            agent_registry[agent_name] = AgentCommandSpec(
                command=command_words, resume_mode=resume_value
            )
    return run_name, default_agent, budget_tokens, agent_registry


def _optional_string_value(
    mapping: dict, key: str, key_lines: dict[str, int], prefix: str = ""
) -> str | None:
    """Return mapping[key] when it is a non-empty string, else raise (None = key absent)."""
    value = mapping.get(key)
    if value is None:
        return None
    full_key = f"{prefix}.{key}" if prefix else key
    if not isinstance(value, str) or not value:
        raise MermaidParseError(
            f"frontmatter '{full_key}' must be a non-empty string",
            key_lines.get(full_key, 1),
        )
    return value
