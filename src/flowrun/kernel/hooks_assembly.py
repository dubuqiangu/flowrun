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

``run_flow`` 用**唯一一个**共享 EventLedger 实例持续追加事件（``ledger`` 形参，
见 ``run_flow`` 的"借用/自建"约定）；判定侧**绝不**再开第二个账本实例。因此判定
侧账本用 ``_JudgeLedgerRecorder``：读路径直接透传**共享账本**的条目（引擎已落账的
目录即时可见），写路径只记录不落盘——判定 tripped 时引擎本来就会落自己的
``gate_tripped(gate="verdict")``（同"暂停转人审"语义），账本链条保持单写者。
被吞写的判定侧事件保留在 recorder 里供审计/测试取证。

## 判定证据转换

"判定节点上游证据 → 域D1 EvidencePack"的转换已拆到独立模块
``kernel.evidence_conversion``（见该模块 docstring）；本模块再导出
``build_judge_evidence_pack`` 以保持既有 import 路径不变。

## 审计产物归位

所有派发审计产物落 ``workspace/audit/``（规范名 ``<node_id>-<kind>-<attempt>``，
命名真相在 ``adapters.base``）；attempt 由账本 ``agent_dispatched`` 条数重建，
跨进程/恢复后读侧仍能凭账本定位同一文件。
"""
from __future__ import annotations

from pathlib import Path

from flowrun.adapters.base import (
    AUDIT_DIRECTORY_NAME,
    AUDIT_KIND_AGENT,
    AUDIT_KIND_NO_ADAPTER,
    AuditArtifact,
    DispatchResult,
    dispatch_attempt_from_ledger_entries,
)
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
    """judge_decision 的账本代理：读透传共享账本条目，写只记录不落盘。

    与共享账本共用**同一个** EventLedger 实例（绝不再开只读视图/第二个写者），
    因此读取即时可见引擎已落账的条目，且不产生任何写副作用。
    """

    def __init__(self, ledger: EventLedger) -> None:
        self._ledger = ledger
        self.recorded_events: list[dict] = []

    def entries(self) -> list:
        """透传共享账本条目（只读；不产生写副作用）。"""
        return self._ledger.entries()

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

    ``ledger`` 是本次运行**唯一**的共享 EventLedger：判定侧的只读视图直接透传
    它的条目；本装配绝不通过任何实例向共享账本追加（单一写者纪律）。
    """
    workspace = Path(workspace)
    audit_directory = workspace / AUDIT_DIRECTORY_NAME
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
        # attempt 由账本重建（执行器在投递前已落 agent_dispatched）。
        attempt = dispatch_attempt_from_ledger_entries(ledger.entries(), node.node_id)
        if agent_adapter is None:
            return _unavailable_agent_result(node, agent_name, audit_directory, attempt)
        effective_timeout = (
            float(timeout_seconds)
            if timeout_seconds is not None
            else float(dispatch_timeout_seconds)
        )
        return await agent_adapter.dispatch(
            Path(packet_path), resolve_command(agent_name), work_dir, effective_timeout,
            audit_artifact=AuditArtifact(
                audit_directory, node.node_id, AUDIT_KIND_AGENT, attempt
            ),
        )

    async def judge_decision_hook(
        flow_ir_arg, decision_node, engine_evidence, work_dir, packet_dir, timeout_seconds
    ):
        ledger_recorder = _JudgeLedgerRecorder(ledger)
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


def _unavailable_agent_result(
    node, agent_name: str, audit_directory: Path, attempt: int
) -> DispatchResult:
    """防御分支：probe 通过但适配器缺失（理论不可达）时给出可审计的失败结果。

    审计落 ``workspace/audit/<node_id>-no-adapter-<attempt>.stdout.log``（与适配器
    正常投递同目录、同命名函数，只是类别段为 ``no-adapter``）。
    """
    audit_artifact = AuditArtifact(
        Path(audit_directory), node.node_id, AUDIT_KIND_NO_ADAPTER, attempt
    )
    stdout_path = audit_artifact.stdout_path()
    stderr_path = audit_artifact.stderr_path()
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text(
        f"agent '{agent_name}' has no usable adapter or command\n", encoding="utf-8"
    )
    stderr_path.write_text("", encoding="utf-8")
    return DispatchResult(
        exit_code=None,
        timed_out=False,
        semantic_done=False,
        stdout_path=stdout_path,
        duration_seconds=0.0,
        stderr_path=stderr_path,
    )
