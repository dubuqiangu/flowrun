"""Convert a parsed FlowchartDocument into FlowIR and validate graph invariants.

Implements tasks.md T003:
- graph connectivity (unreachable nodes are errors)
- multi-in-edge nodes must declare a 'when: all' barrier; such nodes are
  marked kind='join' (implicit join, interfaces.md §2)
- task nodes must not declare an empty 'out=' contract (decision/human/join
  nodes may have no contract)
- 'out=' paths must stay inside the workspace: relative, no '..' segments,
  no drive letter / absolute path / root-relative form (a leading path
  separator resolves against the drive root, not the workspace), and no ':'
  (NTFS alternate data streams address a hidden stream on the same entry) (a
  path outside the workspace turns the existence check into an oracle and lets
  an external file satisfy the contract)
- node agent= references must exist in the frontmatter agent registry
- decision nodes need at least one outgoing edge with a non-'all' 'when:' condition
- self-loop edges (source == target) are rejected
- 'timeout:' edges: at most one per source node, never into a 'join' or
  'decision' node, never combined with 'retry: N'
- the flow must have at least one entry node (a node without incoming edges)
- every edge endpoint (including timeout edges) must reference a defined node
- no agent may pair a known shell command head with content injection (that shell
  would re-tokenize the packet body carried in argv)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

from flowrun.adapters.registry import (
    ShellCommandHeadContentInjectionError,
    require_safe_injection_for_declared_agent,
)
from flowrun.compiler.mermaid_parser import (
    FlowchartDocument,
    ParsedEdge,
    parse_flowchart,
)
from flowrun.ir.schema import (
    NODE_KIND_DECISION,
    NODE_KIND_HUMAN,
    NODE_KIND_JOIN,
    NODE_KIND_TASK,
    EdgeSpec,
    FlowIR,
    NodeSpec,
)

DEFAULT_BUDGET_TOKENS = 1_000_000
WHEN_ALL = "all"


@dataclass
class ValidationError:
    """One validation finding, tied to a Mermaid source line."""

    line_number: int
    message: str

    def __str__(self) -> str:
        return f"line {self.line_number}: {self.message}"


class FlowValidationError(ValueError):
    """Raised by build_flow_ir when the flow graph is invalid."""

    def __init__(self, errors: list[ValidationError]):
        self.errors = list(errors)
        error_summary = "; ".join(str(error) for error in self.errors)
        super().__init__(f"flow validation failed with {len(self.errors)} error(s): {error_summary}")


def collect_validation_errors(document: FlowchartDocument) -> list[ValidationError]:
    """Return every validation finding for the document (empty list = valid)."""
    return _analyze_document(document)[1]


def build_flow_ir(document: FlowchartDocument) -> FlowIR:
    """Convert a parse tree into FlowIR; raises FlowValidationError on a bad graph."""
    flow_ir, errors = _analyze_document(document)
    if errors:
        raise FlowValidationError(errors)
    assert flow_ir is not None  # only reachable when the graph is valid
    return flow_ir


def compile_flow_source(source_text: str) -> FlowIR:
    """Full compiler pipeline: flow.mmd source text -> validated FlowIR."""
    return build_flow_ir(parse_flowchart(source_text))


def _analyze_document(document: FlowchartDocument) -> tuple[FlowIR | None, list[ValidationError]]:
    errors: list[ValidationError] = []

    def report(line_number: int, message: str) -> None:
        errors.append(ValidationError(line_number=line_number, message=message))

    metadata_line = document.frontmatter_line_number or document.declaration_line_number or 1
    if not document.run_name:
        report(metadata_line, "frontmatter must declare 'run: <flow-name>'")
    if document.default_agent is None:
        report(metadata_line, "frontmatter must declare 'defaults: {agent: <name>, ...}'")
    elif document.default_agent not in document.agent_registry:
        report(
            metadata_line,
            f"default agent '{document.default_agent}' is not registered in frontmatter 'agents'",
        )
    # 注入策略（判定在域C 的 registry，报告在域A 的校验器）：命令头是已知 shell 而
    # packet 走 content 注入 = 正文本会作为位置参数进入 argv 被该 shell 重分词。
    # 逐 agent 调用以便一次给出全部违规；非 shell 命令头由纯名字预筛短路，不做
    # PATH 探测（见 registry.require_safe_injection_for_declared_agent）。
    for agent_name, agent_spec in document.agent_registry.items():
        try:
            require_safe_injection_for_declared_agent(agent_name, agent_spec)
        except ShellCommandHeadContentInjectionError as injection_error:
            report(metadata_line, str(injection_error))

    incoming_edges: dict[str, list[ParsedEdge]] = {node_id: [] for node_id in document.nodes}
    outgoing_edges: dict[str, list[ParsedEdge]] = {node_id: [] for node_id in document.nodes}
    for edge in document.edges:
        for endpoint_id in (edge.source_id, edge.target_id):
            if endpoint_id not in document.nodes:
                report(
                    edge.line_number,
                    f"edge references undefined node '{endpoint_id}'",
                )
        if edge.source_id == edge.target_id:
            # 自环：既会伪装成"多入边"，又会让 'when: all' 自环变成永不满足的
            # join。直接拒掉，且不登记进出边（避免连带的假多入边/假死锁报告）。
            report(
                edge.line_number,
                f"self-loop edge '{edge.source_id} --> {edge.target_id}' is not "
                "supported: a node may not be its own predecessor",
            )
            continue
        if edge.source_id in outgoing_edges:
            outgoing_edges[edge.source_id].append(edge)
        if edge.target_id in incoming_edges:
            incoming_edges[edge.target_id].append(edge)

    for node_id, reference_line in document.bare_references.items():
        if node_id not in document.nodes:
            report(
                reference_line,
                f"referenced node '{node_id}' is never defined "
                f"(expected a definition like '{node_id}[label]')",
            )

    join_node_ids: set[str] = set()
    for node_id, node in document.nodes.items():
        if len(incoming_edges[node_id]) < 2:
            continue
        barrier_declared = any(
            edge.when == WHEN_ALL
            for edge in incoming_edges[node_id] + outgoing_edges[node_id]
        )
        if not barrier_declared:
            report(
                node.line_number,
                f"node '{node_id}' has multiple incoming edges but no 'when: all' barrier: "
                "a join must declare 'when: all' on at least one incoming or outgoing edge",
            )
        elif node.shape == NODE_KIND_TASK:
            join_node_ids.add(node_id)

    _report_timeout_edge_violations(document, join_node_ids, report)

    for node_id, node in document.nodes.items():
        if node.agent is not None and node.agent not in document.agent_registry:
            report(
                node.line_number,
                f"node '{node_id}' references agent '{node.agent}' "
                "which is not registered in frontmatter 'agents'",
            )
        for output_path in node.outputs or []:
            path_violation = _describe_invalid_output_path(output_path)
            if path_violation is not None:
                report(
                    node.line_number,
                    f"node '{node_id}' declares output path '{output_path}' that "
                    f"{path_violation}",
                )
        if (
            node.shape == NODE_KIND_TASK
            and node_id not in join_node_ids
            and node.outputs == []
        ):
            report(
                node.line_number,
                f"task node '{node_id}' declares 'out=' with an empty artifact contract",
            )
        if node.shape == NODE_KIND_DECISION:
            has_conditional_out_edge = any(
                edge.when is not None and edge.when != WHEN_ALL
                for edge in outgoing_edges[node_id]
            )
            if not has_conditional_out_edge:
                report(
                    node.line_number,
                    f"decision node '{node_id}' needs at least one outgoing edge labelled "
                    "with a non-'all' 'when:' condition",
                )

    entry_node_ids = sorted(
        node_id for node_id in document.nodes if not incoming_edges[node_id]
    )
    declaration_line = document.declaration_line_number or metadata_line
    if not entry_node_ids:
        report(
            declaration_line,
            "flow has no entry node: every node has incoming edges",
        )
    else:
        reachable_node_ids = _collect_reachable_node_ids(entry_node_ids, outgoing_edges)
        for node_id, node in document.nodes.items():
            if node_id not in reachable_node_ids:
                report(
                    node.line_number,
                    f"node '{node_id}' is unreachable from any entry node",
                )

    errors.sort(key=lambda validation_error: (validation_error.line_number, validation_error.message))
    if errors:
        return None, errors

    nodes: dict[str, NodeSpec] = {}
    for node_id, node in document.nodes.items():
        nodes[node_id] = NodeSpec(
            node_id=node_id,
            kind=NODE_KIND_JOIN if node_id in join_node_ids else node.shape,
            label=node.label,
            agent=node.agent,
            outputs=node.outputs if node.outputs is not None else [],
            line_number=node.line_number,
        )
    edges = [
        EdgeSpec(
            source_id=edge.source_id,
            target_id=edge.target_id,
            when=edge.when,
            retry=edge.retry,
            timeout_seconds=edge.timeout_seconds,
            line_number=edge.line_number,
        )
        for edge in document.edges
    ]
    flow_ir = FlowIR(
        run_name=document.run_name or "",
        default_agent=document.default_agent or "",
        budget_tokens=(
            document.budget_tokens
            if document.budget_tokens is not None
            else DEFAULT_BUDGET_TOKENS
        ),
        agent_registry=dict(document.agent_registry),
        nodes=nodes,
        edges=edges,
        entry_node_ids=entry_node_ids,
    )
    return flow_ir, []


def _report_timeout_edge_violations(
    document: FlowchartDocument,
    join_node_ids: set[str],
    report,
) -> None:
    """FIX-10: 'timeout:' edges must be a single, terminal fallback out of a source node."""
    first_timeout_line_by_source: dict[str, int] = {}
    for edge in document.edges:
        if edge.timeout_seconds is None:
            continue
        target_node = document.nodes.get(edge.target_id)
        if target_node is not None:
            target_kind = (
                NODE_KIND_JOIN if edge.target_id in join_node_ids else target_node.shape
            )
            if target_kind in (NODE_KIND_JOIN, NODE_KIND_DECISION, NODE_KIND_HUMAN):
                report(
                    edge.line_number,
                    f"'timeout:' edge to '{edge.target_id}' must not target a "
                    f"'{target_kind}' node: the timeout path would bypass the "
                    "barrier / judge / approval",
                )
        if edge.retry > 0:
            report(
                edge.line_number,
                f"'timeout:' edge to '{edge.target_id}' must not be combined with "
                f"'retry: {edge.retry}': a timeout is already the terminal fallback",
            )
        first_timeout_line = first_timeout_line_by_source.get(edge.source_id)
        if first_timeout_line is None:
            first_timeout_line_by_source[edge.source_id] = edge.line_number
        else:
            report(
                edge.line_number,
                f"source node '{edge.source_id}' has a second 'timeout:' edge "
                f"(first at line {first_timeout_line}): at most one 'timeout:' edge "
                "per source node",
            )


def _describe_invalid_output_path(output_path: str) -> str | None:
    """FIX-4: return why an 'out=' path is unsafe, or None when it stays in the workspace.

    四类都要拦：POSIX 绝对路径（``/etc/passwd``）、Windows 绝对路径
    （``C:\\...``）、盘符相对（``C:x``）、**根相对**（``\\x`` / ``\\Windows\\win.ini``）。
    最后一类最隐蔽：``PureWindowsPath(r"\\x").is_absolute()`` 是 False、``.drive``
    也为空，但它带 ``.root``（盘根）——``workspace / r"\\Windows\\win.ini"`` 会解析
    到盘根 ``C:\\Windows\\win.ini``，契约就跑到 workspace 外面去了。
    UNC（``\\\\server\\share``）同样带 root，一并拒掉。
    """
    if not output_path.strip():
        return "is empty"
    windows_path = PureWindowsPath(output_path)
    if (
        PurePosixPath(output_path).is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or bool(windows_path.root)
    ):
        return "must be a relative path (absolute paths escape the workspace)"
    if ":" in output_path:
        # NTFS alternate data stream: 'a.txt:stream' has no drive and is not
        # absolute, yet it names a hidden stream on the same directory entry.
        # This check sits after the drive test on purpose, so 'C:\\x' keeps its
        # 'must be a relative path' message (no regression).
        return (
            "must not contain ':' (an NTFS alternate data stream such as "
            "'file.txt:stream' hides data on the same file entry)"
        )
    normalized_parts = PurePosixPath(output_path.replace("\\", "/")).parts
    if ".." in normalized_parts:
        return "must not contain '..' path segments (they escape the workspace)"
    return None


def _collect_reachable_node_ids(
    entry_node_ids: list[str], outgoing_edges: dict[str, list[ParsedEdge]]
) -> set[str]:
    reachable_node_ids: set[str] = set()
    frontier = list(entry_node_ids)
    while frontier:
        node_id = frontier.pop()
        if node_id in reachable_node_ids:
            continue
        reachable_node_ids.add(node_id)
        for edge in outgoing_edges.get(node_id, []):
            if edge.target_id not in reachable_node_ids:
                frontier.append(edge.target_id)
    return reachable_node_ids
