"""生产装配桥接（域D3）：把域D1 判定组件与域C 适配层接进域D2 EngineHooks。

``build_engine_hooks`` 是 CLI 与测试共用的唯一生产装配点：

- build_task_packet → 域D1 ``packet_builder.build_task_packet``（签名一致，直通）；
- probe_agent → 域C ``registry.probe_available`` 的缓存包装（构建时探测一次）；
- dispatch_task → 命令头命中声明式注册表（``create_adapter_for_command``）则用
  注册适配器，否则退 ``FallbackAdapter``（dispatch 签名一致，design.md §2.2
  spec-kit 薄适配器模式）。work_dir 从 packet 路径反推（packets/ 的父目录，
  与引擎内部布局约定一致）；
- judge_decision → EngineEvidencePack → 域D1 EvidencePack 的结构转换
  （两者是结构不同的独立类：引擎侧是"判定节点 + 上游证据列表"，域D1 侧是
  "被评估单节点"），再走域D1 ``judge.judge_decision`` 完整判定协议。

## 单一写者纪律（关键设计）

``run_flow`` 内部自建 EventLedger 实例并持续追加事件；若把另一个会追加的
EventLedger 实例交给 judge_decision，其 ``gate_tripped`` 追加会与引擎实例的
seq 缓存交错——磁盘出现重复 seq / 断链，恢复路径必炸。因此判定侧账本用
``_JudgeLedgerRecorder``：读路径透传当前磁盘账本（每次判定调用时新鲜构造，
读到引擎已落账的条目）；写路径只记录不落盘——判定 tripped 时引擎本来就会
落自己的 ``gate_tripped(gate="verdict")``（同"暂停转人审"语义），账本链条
保持单写者。被吞写的判定侧事件保留在 recorder 里供审计/测试取证。

## 判定证据转换

判定节点的直接前驱常是 join（fork→join→判定），join 自身无投递事件，
其证据是空的；本模块把 join 上溯展开为它的各前驱任务的证据（退出码取
ledger 最近一次 agent_exited、产物契约按工作区现状逐项核对），使判定
packet 携带真实的上游证据。多个证据行合并进域D1 单节点 EvidencePack 时：
产物键加 "节点ID:" 前缀、执行摘要逐节点写进 audit_tail，合并文本再做一次
2000 字符总量截断（保尾部 + 省略标记，FIX-B5——单路读取时的截断挡不住
k 路合并后的 k×2000）。已知限制：引擎侧证据不含 duration_seconds（恒 0）；
账本不记 stdout 审计文件名，展开行的 audit_tail 为空。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from flowrun.adapters.base import DispatchResult
from flowrun.adapters.registry import create_adapter_for_command, probe_available
from flowrun.ir.schema import NODE_KIND_JOIN, FlowIR, NodeSpec
from flowrun.kernel.engine import EngineHooks
from flowrun.kernel.fallback_adapter import FallbackAdapter
from flowrun.kernel.judge import EvidencePack, judge_decision as run_judge_protocol
from flowrun.kernel.packet_builder import build_task_packet as build_task_packet_file
from flowrun.state.ledger import EventLedger

__all__ = [
    "DEFAULT_DISPATCH_TIMEOUT_SECONDS",
    "build_engine_hooks",
    "build_judge_evidence_pack",
]

DEFAULT_DISPATCH_TIMEOUT_SECONDS = 3600.0

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


class _JudgeLedgerRecorder:
    """judge_decision 的账本代理：读透传当前磁盘账本，写只记录不落盘。"""

    def __init__(self, ledger_path: Path) -> None:
        self.ledger_path = Path(ledger_path)
        self.recorded_events: list[dict] = []
        self._read_view: EventLedger | None = None

    def entries(self) -> list:
        """透传当前磁盘账本条目（本代理生命周期内只开一次只读视图，不产生写副作用）。"""
        if self._read_view is None:
            self._read_view = EventLedger.open_readonly(self.ledger_path)
        return self._read_view.entries()

    def append(
        self,
        event_type: str,
        payload: dict,
        idem_key: str,
        run_name: str,
        node_id: str | None = None,
    ) -> None:
        """记录判定侧想落的事件（如 gate_tripped）但不写盘，见模块 docstring。"""
        self.recorded_events.append(
            {
                "type": event_type,
                "payload": dict(payload),
                "idem_key": idem_key,
                "run_name": run_name,
                "node_id": node_id,
            }
        )


def build_engine_hooks(
    flow_ir: FlowIR,
    workspace: Path,
    ledger: EventLedger,
    dispatch_timeout_seconds: float = DEFAULT_DISPATCH_TIMEOUT_SECONDS,
) -> EngineHooks:
    """组装生产 EngineHooks（各注入点的职责见模块 docstring）。

    ``ledger`` 只取其账本路径作为判定侧的只读视图来源；本装配绝不通过任何
    实例向共享账本追加（单一写者纪律）。
    """
    workspace = Path(workspace)
    ledger_path = Path(ledger.ledger_path)
    agent_availability = probe_available(flow_ir.agent_registry)
    fallback_adapter = FallbackAdapter()
    adapter_by_agent_name: dict[str, object] = {}

    def resolve_adapter(agent_name: str):
        agent_spec = flow_ir.agent_registry.get(agent_name)
        if agent_spec is None or not agent_spec.command:
            return None
        if agent_name not in adapter_by_agent_name:
            adapter_by_agent_name[agent_name] = (
                create_adapter_for_command(agent_spec.command) or fallback_adapter
            )
        return adapter_by_agent_name[agent_name]

    def resolve_command(agent_name: str) -> list[str]:
        agent_spec = flow_ir.agent_registry.get(agent_name)
        return list(agent_spec.command) if agent_spec is not None else []

    def probe_agent(agent_name: str) -> bool:
        return bool(agent_availability.get(agent_name, False))

    def build_task_packet(flow_ir_arg, node, work_dir, packet_dir) -> Path:
        return build_task_packet_file(flow_ir_arg, node, Path(work_dir), Path(packet_dir))

    async def dispatch_task(node, packet_path, timeout_seconds) -> DispatchResult:
        agent_name = node.agent or flow_ir.default_agent
        agent_adapter = resolve_adapter(agent_name)
        work_dir = Path(packet_path).resolve().parent.parent
        if agent_adapter is None:
            return _unavailable_agent_result(node, agent_name, work_dir)
        effective_timeout = (
            float(timeout_seconds)
            if timeout_seconds is not None
            else float(dispatch_timeout_seconds)
        )
        return await agent_adapter.dispatch(
            Path(packet_path), resolve_command(agent_name), work_dir, effective_timeout
        )

    async def judge_decision_hook(
        flow_ir_arg, decision_node, engine_evidence, work_dir, packet_dir, timeout_seconds
    ):
        ledger_recorder = _JudgeLedgerRecorder(ledger_path)
        judge_evidence = build_judge_evidence_pack(
            flow_ir_arg, engine_evidence, Path(work_dir), ledger_recorder.entries()
        )
        return await run_judge_protocol(
            flow_ir_arg,
            decision_node,
            judge_evidence,
            Path(work_dir),
            Path(packet_dir),
            ledger_recorder,
            resolve_adapter,
            resolve_command,
            timeout_seconds,
        )

    return EngineHooks(
        build_task_packet=build_task_packet,
        probe_agent=probe_agent,
        dispatch_task=dispatch_task,
        judge_decision=judge_decision_hook,
    )


# ---------------------------------------------------------------- 证据转换


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
        audit_tail=_read_audit_tail(work_dir, upstream.stdout_file_name),
    )


def _row_from_ledger(source_node: NodeSpec, work_dir: Path, ledger_entries: list) -> _EvidenceRow:
    """从账本现算某前驱任务的证据行（join 展开路径用，重放等价）。"""
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
        audit_tail="",
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


# ---------------------------------------------------------------- 内部工具


def _latest_event_payload(ledger_entries: list, event_type: str, node_id: str) -> dict | None:
    """账本中某节点最近一次该类型事件的 payload（倒序找第一条）。"""
    for entry in reversed(ledger_entries):
        if entry.type == event_type and entry.node == node_id:
            return entry.payload
    return None


def _read_audit_tail(work_dir: Path, stdout_file_name: str | None) -> str:
    """读上游 agent 的 stdout 审计尾部（约末 2000 字符）；不可得返回空串。"""
    if not stdout_file_name:
        return ""
    try:
        audit_text = (work_dir / stdout_file_name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return audit_text[-_AUDIT_TAIL_MAX_CHARS:]


def _unavailable_agent_result(node, agent_name: str, work_dir: Path) -> DispatchResult:
    """防御分支：probe 通过但适配器缺失（理论不可达）时给出可审计的失败结果。"""
    work_dir.mkdir(parents=True, exist_ok=True)
    audit_path = work_dir / f"{node.node_id}-no-adapter.stdout.log"
    audit_path.write_text(
        f"agent '{agent_name}' has no usable adapter or command\n", encoding="utf-8"
    )
    return DispatchResult(
        exit_code=None,
        timed_out=False,
        semantic_done=False,
        stdout_path=audit_path,
        duration_seconds=0.0,
    )
