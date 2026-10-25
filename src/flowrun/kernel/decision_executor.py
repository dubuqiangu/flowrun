"""FlowRun 域D2 · 判定（decision）节点执行器（从 engine_node_executors 拆出）。

判定节点：probe → 证据包 → judge（出边定时器为超时）→ verdict → 边 / 闸。

T020a（判定消耗入账）：判定是一次真实 agent 投递，其 token 成本必须与 task 路径
同口径可见——判定的投递事实由 judge 协议回传（``VerdictView.dispatch_facts``），
本模块在 ``agent_dispatched`` 之后、``verdict`` 之前落 ``agent_exited``
（payload 带 ``tokens``），FlowState 的通用 token 累加随即把它计入
``token_spent()``，budget 闸因此不再对判定成本失明。**不新增事件类型**。

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

T020l（edge 口径统一）：``verdict.payload["edge"]`` 记**边在 ``flow_ir.edges`` 中的
声明序号**（与 ``edge_taken`` 幂等键的 ``index`` 同口径），不再是命中边的目标节点名
——同一份账本里不再并存两种边口径，下游不必反查，同源同目标并行边也各有其位。
**跨版本不兼容**：该字段由字符串变整数，旧账本按目标名解读的读法失效（投影层不读
它，重放/续跑语义不变）。
"""

from __future__ import annotations

import inspect
from pathlib import Path

from flowrun.ir.schema import FlowIR, NodeSpec
from flowrun.state.flow_state import FlowState
from flowrun.state.ledger import EventLedger

from .engine_evidence import (
    JudgeDispatchFacts,
    assemble_evidence_pack,
    normalize_verdict,
)
from .engine_gates import (
    GATE_ILLEGAL_EDGE,
    GATE_VERDICT,
    GateFinding,
    # 边身份（声明序号）**唯一实现**：verdict 与 edge_taken 必须同口径，各自抄一份
    # 就会漂移。本模块只读复用，不改该文件。
    _edge_position_in_flow,
    append_edge_taken,
    append_node_failed,
    count_node_events,
    trip_gate,
)
from .workspace_layout import PACKETS_DIRECTORY_NAME
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
            workspace, workspace / PACKETS_DIRECTORY_NAME, judge_timeout_seconds,
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
    _append_judge_agent_exited(
        ledger, run_name, node_id, attempt, verdict_view.dispatch_facts
    )
    chosen_edge_declaration_index = _chosen_edge_declaration_index(
        flow_ir, node_id, verdict_view.chosen_edge_target
    )
    ledger.append(
        "verdict",
        {
            # T020l：edge 记**边在 flow_ir.edges 中的声明序号**（与 edge_taken 幂等键的
            # index 同口径），不再是目标节点名——下游不必反查目标节点名，同源同目标
            # 并行边也各有其位。tripped / 未命中任何出边时记 None。
            "edge": chosen_edge_declaration_index,
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


def _chosen_edge_declaration_index(
    flow_ir: FlowIR,
    node_id: str,
    chosen_edge_target: str | None,
) -> int | None:
    """命中出边在 ``flow_ir.edges`` 中的声明序号（T020l 统一后的 edge 口径）。

    修复前 ``verdict.payload["edge"]`` 写的是**目标节点名**，而投给判定 agent 的
    候选边编号、以及 ``edge_taken`` 的幂等键用的都是**边的身份**——同一份账本里
    两种口径并存，下游得拿目标名反查边；同源同目标的多条并行边更无法区分。现在
    统一为声明序号（按对象同一性定位，与 ``engine_gates._edge_position_in_flow``
    同一实现），与 ``edge_taken`` 幂等键里的 ``index`` 段完全对齐。

    未命中任何出边（``tripped`` / 目标非法 / 目标为空）返回 None——此时本就没有
    被选中的边，记 None 比记一个会被误读成目标名的字符串更诚实。

    **跨版本不兼容**：``verdict.payload["edge"]`` 的类型从字符串（目标节点名）变为
    整数（声明序号），旧账本的该字段不能再按目标节点名解读；如需升级提示请写明。
    投影层不读该字段（``flow_state`` 的 verdict 投影只看 ``tripped``），故重放/续跑
    语义不变，受影响的是审计读账本的一方。
    """
    if chosen_edge_target is None:
        return None
    chosen_edge = next(
        (
            edge
            for edge in outgoing_edges(flow_ir, node_id)
            if edge.target_id == chosen_edge_target
        ),
        None,
    )
    if chosen_edge is None:
        return None
    return _edge_position_in_flow(flow_ir, chosen_edge)


def _append_judge_agent_exited(
    ledger: EventLedger,
    run_name: str,
    node_id: str,
    attempt: int,
    dispatch_facts: JudgeDispatchFacts | None,
) -> None:
    """判定 agent 的 ``agent_exited`` 入账（tasks.md T020a，与 task 路径同口径）。

    落账位置与 task 路径一致：``agent_dispatched`` 之后、``verdict`` 之前；
    payload 形状一致（``exit_code`` / ``timed_out`` / ``semantic_done``，有用量时
    追加 ``tokens``）；idem 键同样带 attempt 段。``tokens`` 经 FlowState 的通用
    累加进 ``token_spent()``，budget 闸据此**看得见判定成本**（修复前判定消耗只
    进 hop 闸、不计 token）。

    hook 未回传投递事实（``dispatch_facts is None``：探活失败 / 无候选边 / 无适配器
    等"没有真实投递"，或旧 verdict / stub 形态）时不落此事件——没有投递就没有
    退出事件，存量判定路径的账本形状零变化。
    """
    if dispatch_facts is None:
        return
    exit_payload = {
        "exit_code": dispatch_facts.exit_code,
        "timed_out": dispatch_facts.timed_out,
        "semantic_done": dispatch_facts.semantic_done,
    }
    if dispatch_facts.tokens > 0:
        exit_payload["tokens"] = dispatch_facts.tokens
    ledger.append(
        "agent_exited",
        exit_payload,
        f"{run_name}-{node_id}-agent-exited-{attempt}",
        run_name,
        node_id,
    )


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
