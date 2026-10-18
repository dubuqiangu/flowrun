"""FlowRun 域D2 · 判定（decision）节点执行器（从 engine_node_executors 拆出）。

判定节点：probe → 证据包 → judge（出边定时器为超时）→ verdict → 边 / 闸。

FIX-B4 兜底：判定 agent 探活失败落 ``gate_tripped(gate="verdict")`` 转人审
（不投递、不留悬挂 running、异常不穿透——穿透只会被 CLI 误报成"无法读取流程
文件"）。探活失败在落 agent_dispatched **之前**拦下。

judge 调用抛异常（如 agent 不在 PATH 时 spawn 抛 WinError 2）时，节点必须
落到**既有终态**：此处捕获后落 ``node_failed``（payload ``reason`` =
"异常类型: 消息"，截断），节点投影为 failed；其后由既有失败处置策略
（⑤ resolve_failed_node_policy）决定重入队 / retry 闸转人审。修复前这里
只落 verdict 闸、不落 node_failed，节点停留在非终态，靠下一轮的崩溃恢复
把它记成含糊的 "interrupted_by_restart"——判定失败的真实原因就此丢失，
resume 也推进不下去（死胡同）。

本模块从 ``kernel.engine_node_executors`` 原样拆出（纯搬移，签名与语义零变化）；
原路径 ``flowrun.kernel.engine_node_executors`` 仍再导出 ``describe_judge_failure``
与 ``MAX_JUDGE_FAILURE_REASON_LENGTH``。
"""

from __future__ import annotations

import inspect
from pathlib import Path

from flowrun.ir.schema import FlowIR, NodeSpec
from flowrun.state.flow_state import FlowState
from flowrun.state.ledger import EventLedger

from .engine_evidence import assemble_evidence_pack, normalize_verdict
from .engine_gates import (
    GATE_ILLEGAL_EDGE,
    GATE_VERDICT,
    GateFinding,
    append_edge_taken,
    append_node_failed,
    count_node_events,
    trip_gate,
)
from .engine_graph import (
    first_dispatch_timeout,
    outgoing_edges,
    valid_decision_targets,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型标注用（EngineHooks 定义在 engine.py，避免运行期循环导入）
    from .engine import EngineHooks

__all__ = [
    "execute_decision",
    "trip_decision_gate",
    "MAX_JUDGE_FAILURE_REASON_LENGTH",
    "describe_judge_failure",
]


async def execute_decision(
    flow_ir: FlowIR,
    decision_node: NodeSpec,
    flow_state: FlowState,
    ledger: EventLedger,
    run_hooks: "EngineHooks",
    workspace: Path,
    dispatch_results: dict[str, object],
) -> None:
    """判定节点：probe → 证据包 → judge（出边定时器为超时）→ verdict → 边 / 闸。

    FIX-B4 兜底：判定 agent 探活失败落 ``gate_tripped(gate="verdict")`` 转人审
    （不投递、不留悬挂 running、异常不穿透——穿透只会被 CLI 误报成"无法读取流程
    文件"）。探活失败在落 agent_dispatched **之前**拦下。

    judge 调用抛异常（如 agent 不在 PATH 时 spawn 抛 WinError 2）时，节点必须
    落到**既有终态**：此处捕获后落 ``node_failed``（payload ``reason`` =
    "异常类型: 消息"，截断），节点投影为 failed；其后由既有失败处置策略
    （⑤ resolve_failed_node_policy）决定重入队 / retry 闸转人审。修复前这里
    只落 verdict 闸、不落 node_failed，节点停留在非终态，靠下一轮的崩溃恢复
    把它记成含糊的 "interrupted_by_restart"——判定失败的真实原因就此丢失，
    resume 也推进不下去（死胡同）。
    """
    run_name = flow_ir.run_name
    node_id = decision_node.node_id
    judge_agent_name = decision_node.agent or flow_ir.default_agent
    if not run_hooks.probe_agent(judge_agent_name):
        trip_decision_gate(
            ledger, run_name, node_id, GATE_VERDICT,
            f"判定 agent '{judge_agent_name}' 不可用（探活未通过）",
            "判定 agent 必须在 PATH 中可用",
        )
        return
    attempt = count_node_events(ledger, "agent_dispatched", node_id) + 1
    ledger.append(
        "agent_dispatched",
        {"attempt": attempt, "purpose": "judge"},
        f"{run_name}-{node_id}-agent-dispatched-{attempt}",
        run_name,
        node_id,
    )
    evidence_pack = assemble_evidence_pack(
        flow_ir, decision_node, flow_state, ledger, workspace, dispatch_results
    )
    judge_timeout_seconds = first_dispatch_timeout(flow_ir, node_id)
    try:
        verdict = run_hooks.judge_decision(
            flow_ir, decision_node, evidence_pack,
            workspace, workspace / "packets", judge_timeout_seconds,
        )
        if inspect.isawaitable(verdict):
            verdict = await verdict
    except Exception as judge_error:  # 判定执行失败：落 node_failed 终态 + reason，异常绝不穿透
        append_node_failed(
            ledger, run_name, node_id,
            {"exit_code": None, "reason": describe_judge_failure(judge_error)},
        )
        return
    verdict_view = normalize_verdict(verdict)
    ledger.append(
        "verdict",
        {
            "edge": verdict_view.chosen_edge_target,
            "confidence": verdict_view.confidence,
            "evidence": verdict_view.evidence,
            "tripped": verdict_view.tripped,
        },
        f"{run_name}-{node_id}-verdict-{attempt}",
        run_name,
        node_id,
    )
    if verdict_view.tripped:
        trip_decision_gate(
            ledger, run_name, node_id, GATE_VERDICT,
            verdict_view.confidence, "confidence_floor",
        )
        return
    valid_targets = valid_decision_targets(flow_ir, node_id)
    if verdict_view.chosen_edge_target not in valid_targets:
        trip_decision_gate(
            ledger, run_name, node_id, GATE_ILLEGAL_EDGE,
            verdict_view.chosen_edge_target, sorted(valid_targets),
        )
        return
    chosen_edge = next(
        edge
        for edge in outgoing_edges(flow_ir, node_id)
        if edge.target_id == verdict_view.chosen_edge_target
    )
    append_edge_taken(ledger, flow_ir, chosen_edge, chosen_edge.when)


def trip_decision_gate(
    ledger: EventLedger,
    run_name: str,
    node_id: str,
    gate_name: str,
    actual: object,
    limit: object,
) -> None:
    """判定路径的统一闸出口（FIX-B4 兜底 / verdict / illegal_edge 共用）。

    闸的一次性语义（每工作区每闸名至多一次）由 engine_gates.trip_gate 保证。
    """
    trip_gate(ledger, run_name, GateFinding(gate_name, actual, limit), node_id)


MAX_JUDGE_FAILURE_REASON_LENGTH = 500
"""node_failed.payload["reason"] 的长度上限（别把整段堆栈 / 长消息塞进账本）。"""


def describe_judge_failure(judge_error: Exception) -> str:
    """判定调用的失败原因（"异常类型: 消息"，超长则截断）——进 node_failed payload。"""
    failure_reason = f"{type(judge_error).__name__}: {judge_error}"
    if len(failure_reason) > MAX_JUDGE_FAILURE_REASON_LENGTH:
        failure_reason = failure_reason[:MAX_JUDGE_FAILURE_REASON_LENGTH] + "...(已截断)"
    return failure_reason
