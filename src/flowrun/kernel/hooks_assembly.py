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

"判定节点上游证据 → 域D1 EvidencePack"的转换已拆到独立模块
``kernel.evidence_conversion``（见该模块 docstring）；本模块再导出
``build_judge_evidence_pack`` 以保持既有 import 路径不变。
"""
from __future__ import annotations

from pathlib import Path

from flowrun.adapters.base import DispatchResult
from flowrun.adapters.registry import create_adapter_for_command, probe_available
from flowrun.ir.schema import FlowIR
from flowrun.kernel.engine import EngineHooks
from flowrun.kernel.evidence_conversion import (
    _AUDIT_TAIL_MAX_CHARS,
    _MERGED_AUDIT_TRUNCATION_MARKER,
    build_judge_evidence_pack,
)
from flowrun.kernel.fallback_adapter import FallbackAdapter
from flowrun.kernel.judge import judge_decision as run_judge_protocol
from flowrun.kernel.packet_builder import build_task_packet as build_task_packet_file
from flowrun.state.ledger import EventLedger

__all__ = [
    "DEFAULT_DISPATCH_TIMEOUT_SECONDS",
    "build_engine_hooks",
    "build_judge_evidence_pack",
]

DEFAULT_DISPATCH_TIMEOUT_SECONDS = 3600.0


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
