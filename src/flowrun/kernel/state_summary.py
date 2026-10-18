"""FlowRun 域D2 · FlowState 只读文本概览（CLI run/status 输出的投影层）。

``print_state_summary`` 把恢复出的 FlowState 打印成稳定的人类可读概览：流向阶段
（done/halted/paused/in-progress）、各节点状态、待人审节点、token/hop 计数与三闸
阈值、已触发闸与恢复提示。纯只读投影，无任何写通道。

诊断段（两处"可诊断"补强，同样只读、不落任何事件）：

- **失败归因**（T020j）：传入 ``ledger`` 时，逐个 failed 节点打印其最后一条
  ``node_failed`` 的 payload 键值对——审账者不必再回到相邻 ``agent_exited``
  反推"是超时、非零退出还是缺产物"；
- **join 阻塞诊断**（T020k）：对未收口的 join，区分"前驱仍在推进（正常等待）"
  与"前驱已 failed（永远不会到达 → 异常挂起）"。此前两者都只表现为流程静默
  停在 in-progress，没有任何信号。

hop 上限的 "4×节点数" 与闸名口径都属内核语义（见 engine_gates），故与内核同目录。

本模块从 ``flowrun.cli`` 原样拆出（纯搬移，签名与语义零变化；上述诊断段与
可选 ``ledger`` 形参为本轮新增，默认关闭时输出与拆出前逐字相同）。
"""
from __future__ import annotations

from flowrun.ir.schema import NODE_KIND_JOIN, FlowIR
from flowrun.kernel.engine_gates import latest_event_payload
from flowrun.kernel.engine_graph import incoming_edges
from flowrun.state.flow_state import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_SKIPPED,
    FlowState,
)
from flowrun.state.ledger import EventLedger

__all__ = ["print_state_summary"]


def print_state_summary(
    flow_ir: FlowIR | None,
    flow_state: FlowState,
    title: str,
    ledger: EventLedger | None = None,
) -> None:
    """打印节点状态 / 三闸计数 / 待人审节点（只读投影，无任何写通道）。

    ``ledger`` 可选：只在给了账本时才打印失败节点的真实原因（T020j）；缺省
    （``None``）时输出与拆出前完全一致。
    """
    print(f"== {title} ==")
    flow_phase = (
        "done" if flow_state.is_done()
        else "halted" if flow_state.is_halted()
        else "paused" if flow_state.is_paused()
        else "in-progress"
    )
    print(f"flow: {flow_phase}")
    if flow_ir is not None:
        for node_id, node_spec in flow_ir.nodes.items():
            print(f"  {node_id} [{node_spec.kind}] {flow_state.node_status(node_id)}")
    for failure_line in describe_node_failures(flow_ir, flow_state, ledger):
        print(failure_line)
    for blocking_line in describe_join_blocking(flow_ir, flow_state):
        print(blocking_line)
    pending_human_nodes = sorted(flow_state.pending_human_nodes())
    if pending_human_nodes:
        print(f"待人审: {'、'.join(pending_human_nodes)}（勾选 todo.md 该行，或 approve/deny 子命令）")
    budget_tokens = flow_ir.budget_tokens if flow_ir is not None else None
    budget_display = f"/{budget_tokens}" if budget_tokens is not None else "/未知"
    print(f"token_spent: {flow_state.token_spent()}{budget_display}")
    hop_limit = 4 * len(flow_ir.nodes) if flow_ir is not None else "未知"
    print(f"hop_count: {flow_state.hop_count()}/{hop_limit}（上限 = 4×节点数）")
    tripped_gates = flow_state.tripped_gates()
    if tripped_gates:
        print(f"已触发闸: {'、'.join(tripped_gates)}")
    if flow_state.is_paused():
        print(
            "提示: 流已暂停，运行 `flowrun resume`，"
            "或在待恢复节点行的行尾用 HTML 注释标记 @resume 恢复"
        )


def describe_node_failures(
    flow_ir: FlowIR | None, flow_state: FlowState, ledger: EventLedger | None
) -> list[str]:
    """failed 节点的失败归因行（T020j）；缺图或缺账本时返回空列表（纯只读）。

    payload 原样按 ``key=value`` 打印——真实原因已由执行器显式落进 payload，
    此处只做投影，不再反推。
    """
    if flow_ir is None or ledger is None:
        return []
    failure_lines: list[str] = []
    for node_id in flow_ir.nodes:
        if flow_state.node_status(node_id) != STATUS_FAILED:
            continue
        failure_payload = latest_event_payload(ledger, "node_failed", node_id)
        if not failure_payload:
            failure_lines.append(f"失败: {node_id}")
            continue
        payload_text = " ".join(
            f"{key}={value}" for key, value in sorted(failure_payload.items())
        )
        failure_lines.append(f"失败: {node_id}（{payload_text}）")
    return failure_lines


def describe_join_blocking(flow_ir: FlowIR | None, flow_state: FlowState) -> list[str]:
    """未收口 join 的阻塞诊断行（T020k）；缺图时返回空列表。

    两类必须区分（此前二者都是"静默停在 in-progress"）：

    - **正常等待**：未齐前驱都还没到终态且不 failed（仍在推进），下轮可能等齐；
    - **异常挂起**：至少一个前驱已 ``failed``——barrier 只认 done/skipped，
      该前驱永远不会到达，join 永久停在原地。
    """
    if flow_ir is None:
        return []
    blocking_lines: list[str] = []
    for node_id, node_spec in flow_ir.nodes.items():
        if node_spec.kind != NODE_KIND_JOIN:
            continue
        if flow_state.node_status(node_id) in (STATUS_DONE, STATUS_SKIPPED):
            continue
        unresolved_predecessor_ids = sorted(
            {
                edge.source_id
                for edge in incoming_edges(flow_ir, node_id)
                if flow_state.node_status(edge.source_id) not in (STATUS_DONE, STATUS_SKIPPED)
            }
        )
        if not unresolved_predecessor_ids:
            continue
        unreachable_predecessor_ids = [
            predecessor_id
            for predecessor_id in unresolved_predecessor_ids
            if flow_state.node_status(predecessor_id) == STATUS_FAILED
        ]
        if unreachable_predecessor_ids:
            blocking_lines.append(
                f"join 阻塞: {node_id} 的前驱 {unreachable_predecessor_ids} 已 failed，"
                "永远不会到达（异常挂起——见上方失败原因，再决定 resume 或换工作区）"
            )
        else:
            blocking_lines.append(
                f"join 等待: {node_id} 等待前驱 {unresolved_predecessor_ids}（正常推进中）"
            )
    return blocking_lines
