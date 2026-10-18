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

判定执行器本体已拆到 ``kernel.decision_executor``（见该模块 docstring）；本模块
再导出 ``describe_judge_failure`` / ``MAX_JUDGE_FAILURE_REASON_LENGTH`` 以保持既有
import 路径不变。
"""

from __future__ import annotations

import asyncio
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

from .decision_executor import (
    MAX_JUDGE_FAILURE_REASON_LENGTH,
    describe_judge_failure,
    execute_decision,
)
from .engine_evidence import (
    EvidencePack,
    assemble_evidence_pack,
    extract_usage_tokens,
)
from .engine_gates import (
    append_node_failed,
    count_node_events,
)
from .engine_graph import (
    first_dispatch_timeout,
    incoming_edges,
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
            await execute_decision(flow_ir, node, flow_state, ledger, run_hooks, workspace, dispatch_results)
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
        append_node_failed(
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
        append_node_failed(
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
        append_node_failed(
            ledger, run_name, task_node.node_id,
            {
                "exit_code": dispatch_result.exit_code,
                "timed_out": dispatch_result.timed_out,
                # 真实原因显式落账：此前只写 exit_code/timed_out，读账者必须回到
                # 相邻 agent_exited 反推"是超时还是非零退出"。reason 取值与既有
                # 词汇一致（timed_out 见 design.md §4.4 的 node_failed 契约）。
                "reason": "timed_out" if dispatch_result.timed_out else "nonzero_exit",
            },
        )
        return
    missing_outputs = [
        output_path
        for output_path in task_node.outputs
        if not (workspace / output_path).exists()
    ]
    if missing_outputs:
        append_node_failed(
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
