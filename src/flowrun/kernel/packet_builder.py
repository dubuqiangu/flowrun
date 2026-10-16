"""packet 构建（design.md §4.3：混合制，以纯 prompt 为主）。

两种 packet 都写成磁盘文件并返回其路径（投递方需要路径做 __FLOW_PACKET_PATH__
占位符展开与审计）：

- 任务包：frontmatter 只保留机器要读的少数字段（node/run/contract/budget），
  正文给"当下这一步"的任务描述 + 上游产物引用 + 完成后自报要求；
  不塞 DAG（拓扑真相在内核，packet 只给当下这一步，防 agent 越权规划）。
- 判定包：候选出边编号清单（编号 = "{source_id}->{target_id}"）+ 每条边
  when 谓词原文 + 证据包 + 严格 JSON 输出格式要求，供判定 agent 回一个
  结构化 verdict（design.md §5.2）。

所有内容纯文本拼装，无模板引擎；packet 是给 agent 读的不可信边界下游，
本模块只产出文本，绝不解析/执行任何 agent 输出。
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from flowrun.ir.schema import EdgeSpec, FlowIR, NodeSpec

if TYPE_CHECKING:  # 仅类型标注；运行期由 judge.py 反向导入本模块，避免环
    from flowrun.kernel.judge import EvidencePack

__all__ = [
    "edge_number",
    "build_task_packet",
    "build_judge_packet",
]


def edge_number(edge: EdgeSpec) -> str:
    """候选边编号的规范形态："{source_id}->{target_id}"（判定协议的 edge 字段取值域）。

    编号在 packet 里按出边顺序附带 1 起始的序号便于人读，但 verdict.edge
    必须是本函数产出的编号文本，judge.py 的命中校验也以此集合为准。
    """
    return f"{edge.source_id}->{edge.target_id}"


def build_task_packet(
    flow_ir: FlowIR, node: NodeSpec, work_dir: Path, packet_dir: Path
) -> Path:
    """构建任务节点 packet 并落盘，返回 packet 文件路径（design.md §4.3 格式）。

    上游产物引用 = 直接前驱节点声明的 out= 产物中、当前确实存在于 work_dir
    的那部分（尚不存在的不列，防 agent 追一个还没产出的文件）；没有任何
    已存在的上游产物时省略该行。
    """
    upstream_artifact_paths = _collect_existing_upstream_outputs(flow_ir, node, work_dir)

    frontmatter_lines = [
        "---",
        f"node: {node.node_id}",
        f"run: {flow_ir.run_name}",
        f"contract: [{', '.join(node.outputs)}]",
        f"budget_tokens: {flow_ir.budget_tokens}",
        "---",
    ]
    body_lines = [node.label]
    if upstream_artifact_paths:
        body_lines.append("上游产物：" + "、".join(upstream_artifact_paths))
    body_lines.append("完成后自报：改了哪些文件、测试结果。")
    packet_text = "\n".join(frontmatter_lines + body_lines) + "\n"

    packet_path = Path(packet_dir) / f"{node.node_id}.packet.md"
    packet_path.parent.mkdir(parents=True, exist_ok=True)
    packet_path.write_text(packet_text, encoding="utf-8")
    return packet_path


def build_judge_packet(
    flow_ir: FlowIR,
    decision_node: NodeSpec,
    candidate_edges: list[EdgeSpec],
    evidence_pack: "EvidencePack",
    packet_dir: Path,
) -> Path:
    """构建判定节点 packet 并落盘，返回 packet 文件路径。

    candidate_edges 只应含谓词边（when 非空且非 "all" 且非 timeout 边）；
    timeout 边与 barrier 边不是判定候选，由调用方（judge.judge_decision）过滤。

    输出格式要求按冻结协议：严格 JSON
    {"edge":"<边编号>","confidence":<0-1>,"evidence":["..."]}。
    """
    candidate_lines = [
        f"{edge_position}. {edge_number(edge)}  when: {edge.when}"
        + (f"  retry: {edge.retry}" if edge.retry else "")
        for edge_position, edge in enumerate(candidate_edges, start=1)
    ]
    evidence_sections = _render_evidence_sections(evidence_pack)
    packet_text = "\n".join(
        [
            "---",
            f"node: {decision_node.node_id}",
            f"run: {flow_ir.run_name}",
            "kind: decision-judge",
            f"budget_tokens: {flow_ir.budget_tokens}",
            "---",
            "你是流程判定器。只基于下方证据包，从候选出边中选出应当走的一条。",
            "",
            "## 候选出边（edge 字段必须严格取下列边编号之一）",
            *candidate_lines,
            "",
            "## 证据包",
            *evidence_sections,
            "",
            "## 输出要求",
            "只输出一个严格 JSON 对象，不要输出任何其他文本。格式：",
            '{"edge":"<候选边编号>","confidence":<0-1之间的数字>,"evidence":["证据1","证据2"]}',
            "",
        ]
    )

    packet_path = Path(packet_dir) / f"{decision_node.node_id}.judge.md"
    packet_path.parent.mkdir(parents=True, exist_ok=True)
    packet_path.write_text(packet_text, encoding="utf-8")
    return packet_path


def _collect_existing_upstream_outputs(
    flow_ir: FlowIR, node: NodeSpec, work_dir: Path
) -> list[str]:
    """直接前驱节点 out= 产物中当前存在于 work_dir 的路径（保序去重）。"""
    upstream_node_ids = []
    for edge in flow_ir.edges:
        if edge.target_id == node.node_id and edge.source_id not in upstream_node_ids:
            upstream_node_ids.append(edge.source_id)

    existing_output_paths: list[str] = []
    for upstream_node_id in upstream_node_ids:
        upstream_node = flow_ir.nodes.get(upstream_node_id)
        if upstream_node is None:
            continue
        for output_path in upstream_node.outputs:
            if output_path not in existing_output_paths and (Path(work_dir) / output_path).exists():
                existing_output_paths.append(output_path)
    return existing_output_paths


def _render_evidence_sections(evidence_pack: "EvidencePack") -> list[str]:
    """把证据包渲染为判定 packet 的 markdown 段落（逐行拼装，无模板引擎）。"""
    execution_summary = (
        f"- node: {evidence_pack.node_id}"
        f"（exit_code={evidence_pack.exit_code},"
        f" timed_out={evidence_pack.timed_out},"
        f" semantic_done={evidence_pack.semantic_done},"
        f" duration_seconds={evidence_pack.duration_seconds}）"
    )

    contract_lines = []
    for output_path, present in evidence_pack.outputs_present.items():
        contract_lines.append(
            f"- {evidence_pack.node_id} {output_path}: {'存在' if present else '缺失'}"
        )
    if not contract_lines:
        contract_lines.append("-（该节点未声明产物契约）")

    audit_tail_lines = [
        f"  {tail_line}" for tail_line in evidence_pack.audit_tail.splitlines()
    ] or ["  （无输出）"]

    return [
        "### 被评估节点的执行结果",
        execution_summary,
        "",
        "### 产物契约核对",
        *contract_lines,
        "",
        "### Agent 输出尾部（审计摘录）",
        f"- {evidence_pack.node_id}:",
        *audit_tail_lines,
    ]
