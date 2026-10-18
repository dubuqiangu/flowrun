"""FlowRun 域D2 · FlowState 只读文本概览（CLI run/status 输出的投影层）。

``print_state_summary`` 把恢复出的 FlowState 打印成稳定的人类可读概览：流向阶段
（done/halted/paused/in-progress）、各节点状态、待人审节点、token/hop 计数与三闸
阈值、已触发闸与恢复提示。纯只读投影，无任何写通道。

hop 上限的 "4×节点数" 与闸名口径都属内核语义（见 engine_gates），故与内核同目录。

本模块从 ``flowrun.cli`` 原样拆出（纯搬移，签名与语义零变化）。
"""
from __future__ import annotations

from flowrun.ir.schema import FlowIR
from flowrun.state.flow_state import FlowState

__all__ = ["print_state_summary"]


def print_state_summary(
    flow_ir: FlowIR | None, flow_state: FlowState, title: str
) -> None:
    """打印节点状态 / 三闸计数 / 待人审节点（只读投影，无任何写通道）。"""
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
