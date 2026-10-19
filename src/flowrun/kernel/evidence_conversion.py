"""FlowRun 域D3 · 判定证据转换（hooks_assembly 的"证据转换"段独立成模块）。

判定节点的直接前驱常是 join（fork→join→判定），join 自身无投递事件，
其证据是空的；本模块把 join 上溯展开为它的各前驱任务的证据（退出码取
ledger 最近一次 agent_exited、产物契约按工作区现状逐项核对），使判定
packet 携带真实的上游证据。多个证据行合并进域D1 单节点 EvidencePack 时：
产物键加 "节点ID:" 前缀、执行摘要逐节点写进 audit_tail，合并文本再做一次
2000 字符总量截断（保尾部 + 省略标记，FIX-B5——单路读取时的截断挡不住
k 路合并后的 k×2000）。已知限制：引擎侧证据不含 duration_seconds（恒 0）；
join 展开行的 audit_tail 按 "<节点>-<类别>-<attempt>" 命名由账本重建后读取。

本模块从 ``kernel.hooks_assembly`` 原样拆出（纯搬移，签名与语义零变化）；
原路径 ``flowrun.kernel.hooks_assembly`` 仍再导出 ``build_judge_evidence_pack``。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from flowrun.ir.schema import NODE_KIND_JOIN, FlowIR, NodeSpec
from flowrun.kernel.engine_evidence import audit_relative_path_for_node
from flowrun.kernel.judge import EvidencePack

__all__ = [
    "build_judge_evidence_pack",
]

# 域D1 EvidencePack.audit_tail 的摘录上限（对齐 judge.py docstring 的 ~2000 字符）
_AUDIT_TAIL_MAX_CHARS = 2000

# 多路上游合并超限时的省略标记（FIX-B5：合并是单路截断之后的第二道总量防线）
_MERGED_AUDIT_TRUNCATION_MARKER = "…（多路上游证据合并后超长，仅保留末尾）\n"

# 多上游合并时 EvidencePack.node_id 的标识文本
_MERGED_UPSTREAM_LABEL = "多上游节点"


@dataclass
class _EvidenceRow:
    """单个上游节点的结构化证据行（合并成域D1 EvidencePack 前的中间形态）。"""

    node_id: str
    exit_code: int | None
    timed_out: bool
    semantic_done: bool
    outputs_present: dict[str, bool]
    audit_tail: str


def build_judge_evidence_pack(
    flow_ir: FlowIR,
    engine_evidence,
    work_dir: Path,
    ledger_entries: list,
) -> EvidencePack:
    """引擎侧证据包（判定节点 + 上游证据列表）→ 域D1 单节点 EvidencePack。

    join 前先展开成各前驱任务的证据行；全部行合并为域D1 的单节点形态
    （单行直接映射，多行合并：产物键带节点前缀、逐节点摘要进 audit_tail）。
    """
    evidence_rows = _collect_evidence_rows(flow_ir, engine_evidence, Path(work_dir), ledger_entries)
    if not evidence_rows:
        evidence_rows = [
            _EvidenceRow(
                node_id=engine_evidence.decision_node_id,
                exit_code=None,
                timed_out=False,
                semantic_done=False,
                outputs_present={},
                audit_tail="",
            )
        ]
    if len(evidence_rows) == 1:
        return _row_to_evidence_pack(evidence_rows[0])
    return _merge_rows_to_evidence_pack(engine_evidence.decision_node_id, evidence_rows)


def _collect_evidence_rows(
    flow_ir: FlowIR, engine_evidence, work_dir: Path, ledger_entries: list
) -> list[_EvidenceRow]:
    """逐上游收集证据行；join 上游展开为其各前驱的证据（嵌套 join 递归展开）。"""
    evidence_rows: list[_EvidenceRow] = []
    visited_node_ids: set[str] = set()

    for upstream in engine_evidence.upstream_evidence:
        upstream_node = flow_ir.nodes.get(upstream.node_id)
        if upstream_node is not None and upstream_node.kind == NODE_KIND_JOIN:
            _expand_join_into_rows(
                flow_ir, upstream_node.node_id, work_dir, ledger_entries,
                evidence_rows, visited_node_ids,
            )
        elif upstream.node_id not in visited_node_ids:
            evidence_rows.append(_row_from_engine_upstream(upstream, work_dir))
            visited_node_ids.add(upstream.node_id)
    return evidence_rows


def _expand_join_into_rows(
    flow_ir: FlowIR,
    join_node_id: str,
    work_dir: Path,
    ledger_entries: list,
    evidence_rows: list[_EvidenceRow],
    visited_node_ids: set[str],
) -> None:
    """把 join 节点展开为其各入边来源的证据行（来源仍是 join 则递归）。"""
    visited_node_ids.add(join_node_id)
    for edge in flow_ir.edges:
        if edge.target_id != join_node_id or edge.source_id in visited_node_ids:
            continue
        source_node = flow_ir.nodes.get(edge.source_id)
        if source_node is None:
            continue
        if source_node.kind == NODE_KIND_JOIN:
            _expand_join_into_rows(
                flow_ir, source_node.node_id, work_dir, ledger_entries,
                evidence_rows, visited_node_ids,
            )
            continue
        evidence_rows.append(_row_from_ledger(source_node, work_dir, ledger_entries))
        visited_node_ids.add(source_node.node_id)


def _row_from_engine_upstream(upstream, work_dir: Path) -> _EvidenceRow:
    """引擎侧上游证据 → 证据行（含本进程内可见的 stdout 审计摘录）。"""
    return _EvidenceRow(
        node_id=upstream.node_id,
        exit_code=upstream.exit_code,
        timed_out=bool(upstream.timed_out),
        semantic_done=bool(upstream.semantic_done),
        outputs_present={
            output_path: output_path in upstream.present_outputs
            for output_path in upstream.expected_outputs
        },
        audit_tail=_read_audit_tail(work_dir, upstream.audit_relative_path),
    )


def _row_from_ledger(source_node: NodeSpec, work_dir: Path, ledger_entries: list) -> _EvidenceRow:
    """从账本现算某前驱任务的证据行（join 展开路径用，重放等价）。

    审计 stdout 路径同样由账本重建（``audit_relative_path_for_node``），故 join
    展开的路径也带得上真实的上游 stdout 摘录。
    """
    exit_payload = _latest_event_payload(ledger_entries, "agent_exited", source_node.node_id) or {}
    return _EvidenceRow(
        node_id=source_node.node_id,
        exit_code=exit_payload.get("exit_code"),
        timed_out=bool(exit_payload.get("timed_out")),
        semantic_done=bool(exit_payload.get("semantic_done")),
        outputs_present={
            output_path: (work_dir / output_path).exists()
            for output_path in source_node.outputs
        },
        audit_tail=_read_audit_tail(
            work_dir, audit_relative_path_for_node(source_node, ledger_entries)
        ),
    )


def _row_to_evidence_pack(row: _EvidenceRow) -> EvidencePack:
    """单行直映射为域D1 EvidencePack（duration_seconds 引擎侧不提供，恒 0）。"""
    return EvidencePack(
        node_id=row.node_id,
        exit_code=row.exit_code,
        timed_out=row.timed_out,
        semantic_done=row.semantic_done,
        duration_seconds=0.0,
        outputs_present=dict(row.outputs_present),
        audit_tail=row.audit_tail,
    )


def _merge_rows_to_evidence_pack(decision_node_id: str, rows: list[_EvidenceRow]) -> EvidencePack:
    """多行合并为域D1 单节点 EvidencePack（产物键带节点前缀防路径撞名）。

    单行读取时已各自截到 2000 字符，k 路合并后可达 k×2000——FIX-B5：合并文本
    再做一次总量截断（保尾部 + 省略标记），使 audit_tail 恒 ≤ 2000 字符。
    """
    merged_outputs_present: dict[str, bool] = {}
    for row in rows:
        for output_path, present in row.outputs_present.items():
            merged_outputs_present[f"{row.node_id}:{output_path}"] = present

    audit_lines: list[str] = []
    for row in rows:
        audit_lines.append(
            f"{row.node_id}: exit_code={row.exit_code}, timed_out={row.timed_out}, "
            f"semantic_done={row.semantic_done}"
        )
        audit_lines.extend(row.audit_tail.splitlines())

    exit_codes = {row.exit_code for row in rows}
    return EvidencePack(
        node_id=_MERGED_UPSTREAM_LABEL,
        exit_code=next(iter(exit_codes)) if len(exit_codes) == 1 else None,
        timed_out=any(row.timed_out for row in rows),
        semantic_done=all(row.semantic_done for row in rows),
        duration_seconds=0.0,
        outputs_present=merged_outputs_present,
        audit_tail=_cap_merged_audit_tail("\n".join(audit_lines)),
    )


def _cap_merged_audit_tail(merged_audit_tail: str) -> str:
    """合并后的 audit_tail 总量截断（保尾部）；未超限则原样返回（FIX-B5）。

    省略标记本身占的长度也从总量里扣，保证含标记后总长仍 ≤ _AUDIT_TAIL_MAX_CHARS。
    """
    if len(merged_audit_tail) <= _AUDIT_TAIL_MAX_CHARS:
        return merged_audit_tail
    kept_tail_length = _AUDIT_TAIL_MAX_CHARS - len(_MERGED_AUDIT_TRUNCATION_MARKER)
    return _MERGED_AUDIT_TRUNCATION_MARKER + merged_audit_tail[-kept_tail_length:]


def _latest_event_payload(ledger_entries: list, event_type: str, node_id: str) -> dict | None:
    """账本中某节点最近一次该类型事件的 payload（倒序找第一条）。"""
    for entry in reversed(ledger_entries):
        if entry.type == event_type and entry.node == node_id:
            return entry.payload
    return None


def _read_audit_tail(work_dir: Path, audit_relative_path: str | None) -> str:
    """读上游 agent 的 stdout 审计尾部（约末 2000 字符）；不可得返回空串。

    ``audit_relative_path`` 是相对工作区根的路径（如 ``audit/T001-agent-1.stdout.log``）；
    文件缺失 / 路径为空一律返回空串——缺文件不是错误，不报错不崩。
    """
    if not audit_relative_path:
        return ""
    try:
        audit_text = (Path(work_dir) / audit_relative_path).read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return ""
    return audit_text[-_AUDIT_TAIL_MAX_CHARS:]
