"""FlowRun 域D2 · ready 节点执行器（主循环第④步，design.md §5.1 状态机）。

每轮从 ledger 恢复出的 FlowState 取"ready"节点集合（ready 的唯一来源是
ledger 事件——重放等价），轮内用局部 pending 集合防重复调度：

- task：probe → packet → dispatch（asyncio.gather 并发）→ agent_exited
  （structured_result 的 usage 数字进 payload.tokens）→ out= 产物核对 →
  node_done / node_failed；
- decision：probe → 入边来源证据包 → judge hook（出边 timeout_seconds 为其
  超时）→ verdict 落账 → tripped / 非法目标 → 闸；否则 edge_taken（FIX-B4：
  判定 agent 不可用落 verdict 闸转人审，不投递不穿透；judge 调用抛异常则落
  node_failed 终态并给出 reason，由既有失败处置策略接手——不新造事件类型）；
- join：入边前驱全部 resolved（done 或 skipped——skip 视为已越过屏障，
  避免用户跳过某分支后 join 永久死锁）才 node_done（barrier）；
- human：human_requested 落账后本轮即回（run_flow 挂起等待 todo 勾选）。

判定节点投递（judge 调用也是一次 agent 投递）同样落 agent_dispatched——
hop 闸因此能约束"判定↔任务"构成的循环；判定调用的 agent_exited/usage
由域D1 judge 内部掌握，M1 不计入 token 账（见交付报告的已知限制）。
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from flowrun.ir.schema import (
    NODE_KIND_DECISION,
    NODE_KIND_HUMAN,
    NODE_KIND_JOIN,
    NODE_KIND_TASK,
    FlowIR,
    NodeSpec,
)
from flowrun.state.flow_state import STATUS_DONE, STATUS_READY, STATUS_SKIPPED, FlowState
from flowrun.state.ledger import EventLedger

from .engine_evidence import (
    EvidencePack,
    assemble_evidence_pack,
    extract_usage_tokens,
    normalize_verdict,
)
from .engine_gates import (
    GATE_ILLEGAL_EDGE,
    GATE_VERDICT,
    GateFinding,
    append_edge_taken,
    count_node_events,
    trip_gate,
)
from .engine_graph import (
    first_dispatch_timeout,
    incoming_edges,
    outgoing_edges,
    valid_decision_targets,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型标注用（EngineHooks 定义在 engine.py，避免运行期循环导入）
    from .engine import EngineHooks

__all__ = [
    "advance_ready_nodes",
    "assemble_evidence_pack",
    "EvidencePack",
]


async def advance_ready_nodes(
    flow_ir: FlowIR,
    flow_state: FlowState,
    ledger: EventLedger,
    run_hooks: "EngineHooks",
    workspace: Path,
    dispatch_results: dict[str, object],
) -> bool:
    """推进本轮全部 ready 节点；返回 True 表示已落 human_requested（本轮挂起）。

    task 节点并发执行（asyncio.gather）；decision/join/human 顺序处理。
    ready 集合取自轮首恢复的 FlowState 快照，轮内新变 ready 的节点留给下一轮。
    """
    ready_node_ids = [
        node_id
        for node_id in flow_ir.nodes
        if flow_state.node_status(node_id) == STATUS_READY
    ]
    pending_node_ids: set[str] = set()  # 轮内防重复调度（局部，不落账）
    task_nodes: list[NodeSpec] = []
    for node_id in ready_node_ids:
        if flow_ir.nodes[node_id].kind == NODE_KIND_TASK:
            pending_node_ids.add(node_id)
            task_nodes.append(flow_ir.nodes[node_id])
    if task_nodes:
        await asyncio.gather(
            *(
                _execute_task(flow_ir, task_node, ledger, run_hooks, workspace, dispatch_results)
                for task_node in task_nodes
            )
        )

    human_wait_requested = False
    for node_id in ready_node_ids:
        if node_id in pending_node_ids:
            continue
        node = flow_ir.nodes[node_id]
        if node.kind == NODE_KIND_DECISION:
            pending_node_ids.add(node_id)
            await _execute_decision(flow_ir, node, flow_state, ledger, run_hooks, workspace, dispatch_results)
        elif node.kind == NODE_KIND_JOIN:
            pending_node_ids.add(node_id)
            _execute_join(flow_ir, node, flow_state, ledger)
        elif node.kind == NODE_KIND_HUMAN:
            pending_node_ids.add(node_id)
            _request_human_review(flow_ir, node, ledger)
            human_wait_requested = True
    return human_wait_requested


# ---------------------------------------------------------------- 各类节点执行


async def _execute_task(
    flow_ir: FlowIR,
    task_node: NodeSpec,
    ledger: EventLedger,
    run_hooks: "EngineHooks",
    workspace: Path,
    dispatch_results: dict[str, object],
) -> None:
    """任务节点：probe → packet → dispatch → 双信号退出 → 产物核对 → done/failed。"""
    run_name = flow_ir.run_name
    agent_name = task_node.agent or flow_ir.default_agent
    if not run_hooks.probe_agent(agent_name):
        _append_node_failed(
            ledger, run_name, task_node.node_id,
            {"exit_code": None, "reason": "agent_unavailable", "agent": agent_name},
        )
        return
    attempt = count_node_events(ledger, "agent_dispatched", task_node.node_id) + 1
    packet_path = run_hooks.build_task_packet(
        flow_ir, task_node, workspace, workspace / "packets"
    )
    ledger.append(
        "agent_dispatched",
        {"packet": Path(packet_path).name, "attempt": attempt},
        f"{run_name}-{task_node.node_id}-agent-dispatched-{attempt}",
        run_name,
        task_node.node_id,
    )
    dispatch_timeout_seconds = first_dispatch_timeout(flow_ir, task_node.node_id)
    try:
        dispatch_result = await run_hooks.dispatch_task(
            task_node, Path(packet_path), dispatch_timeout_seconds
        )
    except Exception as dispatch_error:
        # spawn 失败（如可执行缺失/被拒）等一切投递异常：记 node_failed 而非炸引擎，
        # 走该节点正常的 retry/失败处置策略（与 agent_exited 后的失败同路）。
        _append_node_failed(
            ledger, run_name, task_node.node_id,
            {"exit_code": None, "reason": "dispatch_exception",
             "error": str(dispatch_error)},
        )
        return
    dispatch_results[task_node.node_id] = dispatch_result
    exit_payload = {
        "exit_code": dispatch_result.exit_code,
        "timed_out": dispatch_result.timed_out,
        "semantic_done": dispatch_result.semantic_done,
    }
    usage_tokens = extract_usage_tokens(dispatch_result.structured_result)
    if usage_tokens > 0:
        exit_payload["tokens"] = usage_tokens
    ledger.append(
        "agent_exited",
        exit_payload,
        f"{run_name}-{task_node.node_id}-agent-exited-{attempt}",
        run_name,
        task_node.node_id,
    )

    succeeded = (
        not dispatch_result.timed_out
        and (dispatch_result.exit_code == 0 or dispatch_result.semantic_done)
    )
    if not succeeded:
        _append_node_failed(
            ledger, run_name, task_node.node_id,
            {"exit_code": dispatch_result.exit_code, "timed_out": dispatch_result.timed_out},
        )
        return
    missing_outputs = [
        output_path
        for output_path in task_node.outputs
        if not (workspace / output_path).exists()
    ]
    if missing_outputs:
        _append_node_failed(
            ledger, run_name, task_node.node_id,
            {"exit_code": dispatch_result.exit_code, "reason": "missing_outputs",
             "missing": missing_outputs},
        )
        return
    ledger.append(
        "node_done",
        {"artifacts": list(task_node.outputs), "attempt": attempt},
        f"{run_name}-{task_node.node_id}-node-done-{attempt}",
        run_name,
        task_node.node_id,
    )


async def _execute_decision(
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
        _trip_gate(
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
        _append_node_failed(
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
        _trip_gate(
            ledger, run_name, node_id, GATE_VERDICT,
            verdict_view.confidence, "confidence_floor",
        )
        return
    valid_targets = valid_decision_targets(flow_ir, node_id)
    if verdict_view.chosen_edge_target not in valid_targets:
        _trip_gate(
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


def _trip_gate(
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


def _execute_join(
    flow_ir: FlowIR,
    join_node: NodeSpec,
    flow_state: FlowState,
    ledger: EventLedger,
) -> None:
    """join 节点（barrier）：入边前驱全部 resolved（done/skipped）才 node_done。

    前驱未等齐时本方法不落账（节点维持 ready，下轮再查）——若整轮因此
    无任何进展，主循环的无进展判定会挂起返回而不是空转（见 engine.py）。
    """
    predecessor_edges = incoming_edges(flow_ir, join_node.node_id)
    resolved_predecessor_ids = [
        edge.source_id
        for edge in predecessor_edges
        if flow_state.node_status(edge.source_id) in (STATUS_DONE, STATUS_SKIPPED)
    ]
    if len(resolved_predecessor_ids) < len(predecessor_edges):
        return
    run_name = flow_ir.run_name
    done_occurrence = count_node_events(ledger, "node_done", join_node.node_id) + 1
    ledger.append(
        "node_done",
        {"barrier": resolved_predecessor_ids},
        f"{run_name}-{join_node.node_id}-node-done-{done_occurrence}",
        run_name,
        join_node.node_id,
    )


def _request_human_review(flow_ir: FlowIR, human_node: NodeSpec, ledger: EventLedger) -> None:
    """人审节点：执行到此必停——落 human_requested，等 todo 勾选或审批通道。"""
    run_name = flow_ir.run_name
    request_occurrence = count_node_events(ledger, "human_requested", human_node.node_id) + 1
    ledger.append(
        "human_requested",
        {},
        f"{run_name}-{human_node.node_id}-human-requested-{request_occurrence}",
        run_name,
        human_node.node_id,
    )


def _append_node_failed(
    ledger: EventLedger, run_name: str, node_id: str, payload: dict
) -> None:
    """落 node_failed（幂等键带失败次数段，重试后的再次失败各自计一条）。"""
    failure_occurrence = count_node_events(ledger, "node_failed", node_id) + 1
    ledger.append(
        "node_failed",
        payload,
        f"{run_name}-{node_id}-node-failed-{failure_occurrence}",
        run_name,
        node_id,
    )


MAX_JUDGE_FAILURE_REASON_LENGTH = 500
"""node_failed.payload["reason"] 的长度上限（别把整段堆栈 / 长消息塞进账本）。"""


def describe_judge_failure(judge_error: Exception) -> str:
    """判定调用的失败原因（"异常类型: 消息"，超长则截断）——进 node_failed payload。"""
    failure_reason = f"{type(judge_error).__name__}: {judge_error}"
    if len(failure_reason) > MAX_JUDGE_FAILURE_REASON_LENGTH:
        failure_reason = failure_reason[:MAX_JUDGE_FAILURE_REASON_LENGTH] + "...(已截断)"
    return failure_reason
