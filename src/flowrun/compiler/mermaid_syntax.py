"""Line-level syntax primitives for the Mermaid flowchart subset.

Everything in this module operates on a single content line: tokenizing
node/edge chains and parsing node segments (shapes + ';;' config). Edge-label
parsing (when/retry/timeout) lives in flowrun.compiler.mermaid_edge_label;
document-level assembly lives in flowrun.compiler.mermaid_parser.

Every error carries the Mermaid source line number. The M1 decision applies
here: a ']' inside ';;' node config is disabled and reported as a
compile-time error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from flowrun.compiler.mermaid_edge_label import parse_edge_label
from flowrun.compiler.mermaid_errors import MermaidParseError
from flowrun.ir.schema import (
    NODE_KIND_DECISION,
    NODE_KIND_HUMAN,
    NODE_KIND_TASK,
)

__all__ = [
    "MermaidParseError",
    "ParsedEdge",
    "ParsedNode",
    "parse_content_line",
]


@dataclass
class ParsedNode:
    """A node definition found in the source (join marking happens in the validator)."""

    node_id: str
    shape: str  # NODE_KIND_TASK | NODE_KIND_DECISION | NODE_KIND_HUMAN
    label: str
    agent: str | None
    outputs: list[str] | None  # None = 'out=' not declared; [] = declared but empty
    line_number: int


@dataclass
class ParsedEdge:
    source_id: str
    target_id: str
    when: str | None
    retry: int
    timeout_seconds: float | None
    line_number: int


@dataclass
class _NodeToken:
    text: str


@dataclass
class _EdgeToken:
    has_arrowhead: bool
    label_text: str | None


_SHAPE_CLOSERS = {"[": "]", "{": "}", "(": ")"}


def parse_content_line(
    line_text: str,
    line_number: int,
    nodes: dict[str, ParsedNode],
    edges: list[ParsedEdge],
    bare_references: dict[str, int],
) -> None:
    """Parse one content line into node definitions and edges (mutates the maps)."""
    tokens = _scan_line_tokens(line_text, line_number)
    if not tokens:
        return
    for token_index, token in enumerate(tokens):
        expected_node_token = token_index % 2 == 0
        if expected_node_token != isinstance(token, _NodeToken):
            raise MermaidParseError(
                "malformed line: nodes and edges must alternate like 'A --> B --> C'",
                line_number,
            )
    node_entries: list[tuple[str, ParsedNode | None]] = []
    for token in tokens:
        if isinstance(token, _NodeToken):
            node_entries.append(_parse_node_segment(token.text, line_number))
    edge_count = len(tokens) // 2
    for edge_index in range(edge_count):
        edge_token = tokens[2 * edge_index + 1]
        when_value, retry_count, timeout_seconds = parse_edge_label(
            edge_token.label_text, line_number
        )
        edges.append(
            ParsedEdge(
                source_id=node_entries[edge_index][0],
                target_id=node_entries[edge_index + 1][0],
                when=when_value,
                retry=retry_count,
                timeout_seconds=timeout_seconds,
                line_number=line_number,
            )
        )
    for node_id, node_definition in node_entries:
        if node_definition is None:
            if edge_count == 0 and node_id not in bare_references:
                bare_references[node_id] = line_number
            continue
        existing_definition = nodes.get(node_id)
        if existing_definition is not None:
            raise MermaidParseError(
                f"node '{node_id}' is defined twice (first definition at line "
                f"{existing_definition.line_number})",
                line_number,
            )
        nodes[node_id] = node_definition


def _scan_line_tokens(line_text: str, line_number: int) -> list:
    """Tokenize a content line into alternating node/edge tokens.

    Shape brackets ([...], {...}, (...)) are tracked so edge arrows and '%%'
    comments inside node labels are treated as literal text.
    """
    tokens: list = []
    segment_start = 0
    position = 0
    open_shape_closers: list[str] = []
    length = len(line_text)
    while position < length:
        char = line_text[position]
        if open_shape_closers:
            if char == open_shape_closers[-1]:
                open_shape_closers.pop()
            elif char in _SHAPE_CLOSERS:
                open_shape_closers.append(_SHAPE_CLOSERS[char])
            position += 1
            continue
        if char in _SHAPE_CLOSERS:
            open_shape_closers.append(_SHAPE_CLOSERS[char])
            position += 1
            continue
        if char == "%" and line_text.startswith("%%", position):
            length = position
            break
        if char == "-" and line_text.startswith("--", position):
            dash_end = position
            while dash_end < length and line_text[dash_end] == "-":
                dash_end += 1
            has_arrowhead = dash_end < length and line_text[dash_end] == ">"
            if has_arrowhead:
                dash_end += 1
            elif dash_end - position < 3:
                raise MermaidParseError(
                    f"invalid edge arrow '{line_text[position:dash_end]}': "
                    "use '-->' (label via |...|), '--->' or '---'",
                    line_number,
                )
            segment_text = line_text[segment_start:position].strip()
            if not segment_text:
                raise MermaidParseError(
                    "edge arrow is missing its source node", line_number
                )
            tokens.append(_NodeToken(text=segment_text))
            label_text: str | None = None
            if dash_end < length and line_text[dash_end] == "|":
                label_closing = line_text.find("|", dash_end + 1)
                if label_closing == -1:
                    raise MermaidParseError(
                        "unterminated edge label: missing closing '|'", line_number
                    )
                label_text = line_text[dash_end + 1 : label_closing].strip()
                dash_end = label_closing + 1
            tokens.append(_EdgeToken(has_arrowhead=has_arrowhead, label_text=label_text))
            position = dash_end
            segment_start = position
            continue
        position += 1
    trailing_text = line_text[segment_start:length].strip()
    if trailing_text:
        tokens.append(_NodeToken(text=trailing_text))
    return tokens


_NODE_ID_PATTERN = re.compile(r"[A-Za-z0-9_]+")


def _parse_node_segment(segment_text: str, line_number: int) -> tuple[str, ParsedNode | None]:
    id_match = _NODE_ID_PATTERN.match(segment_text)
    if id_match is None or id_match.start() != 0:
        raise MermaidParseError(
            f"expected a node id like 'T001', got '{segment_text[:24]}'", line_number
        )
    node_id = id_match.group(0)
    remainder = segment_text[id_match.end() :].strip()
    if not remainder:
        return node_id, None
    if remainder.startswith("["):
        return node_id, _parse_bracketed_node(node_id, remainder, NODE_KIND_TASK, line_number)
    if remainder.startswith("{"):
        return node_id, _parse_bracketed_node(node_id, remainder, NODE_KIND_DECISION, line_number)
    if remainder.startswith("(["):
        return node_id, _parse_human_node(node_id, remainder, line_number)
    raise MermaidParseError(
        f"unrecognized node shape for '{node_id}': M1 supports '[label]', '{{label}}' "
        f"and '([label])', got '{remainder[:24]}'",
        line_number,
    )


def _parse_bracketed_node(
    node_id: str, remainder: str, shape_kind: str, line_number: int
) -> ParsedNode:
    closing_bracket = "]" if remainder[0] == "[" else "}"
    closing_position = remainder.find(closing_bracket)
    if closing_position == -1:
        raise MermaidParseError(
            f"unterminated '{remainder[0]}' in {shape_kind} node '{node_id}'", line_number
        )
    bracket_content = remainder[1:closing_position]
    trailing_text = remainder[closing_position + 1 :].strip()
    if trailing_text:
        if ";;" in bracket_content:
            raise MermaidParseError(
                f"']' characters inside ';;' node config are not allowed in M1 "
                f"(node '{node_id}', trailing text '{trailing_text[:24]}'); "
                "remove the ']' from the config or drop the config",
                line_number,
            )
        raise MermaidParseError(
            f"unexpected text after node '{node_id}': '{trailing_text[:24]}'", line_number
        )
    label, agent, outputs = _parse_node_config(bracket_content, node_id, line_number)
    return ParsedNode(
        node_id=node_id,
        shape=shape_kind,
        label=label,
        agent=agent,
        outputs=outputs,
        line_number=line_number,
    )


def _parse_human_node(node_id: str, remainder: str, line_number: int) -> ParsedNode:
    bracket_closing_position = remainder.find("]")
    if bracket_closing_position == -1:
        raise MermaidParseError(
            f"unterminated '([ ... ])' in human node '{node_id}'", line_number
        )
    if remainder[bracket_closing_position + 1 : bracket_closing_position + 2] != ")":
        raise MermaidParseError(
            f"human node '{node_id}' must close its shape with '])'", line_number
        )
    bracket_content = remainder[2:bracket_closing_position]
    trailing_text = remainder[bracket_closing_position + 2 :].strip()
    if trailing_text:
        if ";;" in bracket_content:
            raise MermaidParseError(
                f"']' characters inside ';;' node config are not allowed in M1 "
                f"(node '{node_id}', trailing text '{trailing_text[:24]}'); "
                "remove the ']' from the config or drop the config",
                line_number,
            )
        raise MermaidParseError(
            f"unexpected text after node '{node_id}': '{trailing_text[:24]}'", line_number
        )
    label, agent, outputs = _parse_node_config(bracket_content, node_id, line_number)
    return ParsedNode(
        node_id=node_id,
        shape=NODE_KIND_HUMAN,
        label=label,
        agent=agent,
        outputs=outputs,
        line_number=line_number,
    )


def _parse_node_config(
    bracket_content: str, node_id: str, line_number: int
) -> tuple[str, str | None, list[str] | None]:
    """Split 'label;;agent=x;;out=a,b' into (label, agent, outputs)."""
    config_parts = bracket_content.split(";;")
    label = config_parts[0].strip()
    if not label:
        raise MermaidParseError(f"node '{node_id}' has an empty label", line_number)
    agent: str | None = None
    outputs: list[str] | None = None
    for config_item in config_parts[1:]:
        config_item = config_item.strip()
        if not config_item:
            raise MermaidParseError(
                f"node '{node_id}' has an empty ';;' config item", line_number
            )
        config_key, separator, config_value = config_item.partition("=")
        config_key = config_key.strip()
        config_value = config_value.strip()
        if not separator or not config_key:
            raise MermaidParseError(
                f"invalid ';;' config item '{config_item}' on node '{node_id}' "
                "(expected 'agent=...' or 'out=...')",
                line_number,
            )
        if config_key == "agent":
            if not config_value:
                raise MermaidParseError(
                    f"node '{node_id}' declares 'agent=' with an empty value", line_number
                )
            agent = config_value
        elif config_key == "out":
            if not config_value:
                outputs = []  # declared but empty: the validator reports this
            else:
                output_paths = [path.strip() for path in config_value.split(",")]
                if any(not path for path in output_paths):
                    raise MermaidParseError(
                        f"node '{node_id}' has an invalid 'out=' list '{config_value}' "
                        "(comma-separated non-empty paths expected)",
                        line_number,
                    )
                outputs = output_paths
        else:
            raise MermaidParseError(
                f"unknown ';;' config key '{config_key}' on node '{node_id}' "
                "(supported: 'agent=', 'out=')",
                line_number,
            )
    return label, agent, outputs

