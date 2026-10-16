"""FlowRun 域D2 · 内核共享的流程图查询小工具（纯函数，无状态）。

边分类依据（design.md §4.1 / interfaces.md §2）：
- 无条件边：``when=None`` 且无 timeout——源节点 done 即激活；
- join barrier 边：``when="all"``——多入边节点的等齐声明（写在入边或出边上）；
- 条件边：``when=<自然语言谓词>``——只有判定节点的 verdict 能选中它；
- 超时分支边：``when=None`` 且 ``timeout_seconds`` 非空——该边携带源节点的
  执行定时器，定时器先到即走此边（判定器不评估它）。
"""

from __future__ import annotations

from flowrun.ir.schema import EdgeSpec, FlowIR

__all__ = [
    "WHEN_ALL",
    "outgoing_edges",
    "incoming_edges",
    "is_unconditional_edge",
    "is_barrier_edge",
    "is_timeout_edge",
    "is_retry_edge",
    "first_dispatch_timeout",
    "first_timeout_edge",
    "first_retry_edge",
    "valid_decision_targets",
]

WHEN_ALL = "all"


def outgoing_edges(flow_ir: FlowIR, node_id: str) -> list[EdgeSpec]:
    """某节点的全部出边（按 flow_ir.edges 声明顺序，保证轮次间确定性）。"""
    return [edge for edge in flow_ir.edges if edge.source_id == node_id]


def incoming_edges(flow_ir: FlowIR, node_id: str) -> list[EdgeSpec]:
    """某节点的全部入边（按声明顺序）。"""
    return [edge for edge in flow_ir.edges if edge.target_id == node_id]


def is_unconditional_edge(edge: EdgeSpec) -> bool:
    """无条件边：when=None 且不带定时器（done 节点出边激活的对象）。"""
    return edge.when is None and edge.timeout_seconds is None


def is_barrier_edge(edge: EdgeSpec) -> bool:
    """join barrier 边：when="all"。"""
    return edge.when == WHEN_ALL


def is_timeout_edge(edge: EdgeSpec) -> bool:
    """超时分支边：when=None 且 timeout_seconds 非空。"""
    return edge.when is None and edge.timeout_seconds is not None


def is_retry_edge(edge: EdgeSpec) -> bool:
    """重试边：retry > 0（源节点失败后可重新入队重跑）。"""
    return edge.retry > 0


def first_dispatch_timeout(flow_ir: FlowIR, node_id: str) -> float | None:
    """节点执行（agent 投递 / 判定调用）的超时值 = 其出边携带的定时器。

    取声明顺序第一条带 timeout_seconds 的出边；无则不限时。
    判定节点的超时约定即"出边 timeout_seconds 为其 timeout"（任务书 T010），
    任务节点沿用同一约定以保持一致。
    """
    for edge in outgoing_edges(flow_ir, node_id):
        if edge.timeout_seconds is not None:
            return float(edge.timeout_seconds)
    return None


def first_timeout_edge(flow_ir: FlowIR, node_id: str) -> EdgeSpec | None:
    """节点失败且失败原因为超时时，走的超时分支边（无则返回 None）。"""
    for edge in outgoing_edges(flow_ir, node_id):
        if is_timeout_edge(edge):
            return edge
    return None


def first_retry_edge(flow_ir: FlowIR, node_id: str) -> EdgeSpec | None:
    """节点的重试边（声明顺序第一条 retry>0 的出边；无则返回 None）。"""
    for edge in outgoing_edges(flow_ir, node_id):
        if is_retry_edge(edge):
            return edge
    return None


def valid_decision_targets(flow_ir: FlowIR, node_id: str) -> set[str]:
    """判定节点合法的目标集合 = 其全部出边的 target（谓词边 + 超时边）。

    verdict 命中集合之外的目标 = 非法边转移（design.md §5.3 第三道闸）。
    """
    return {edge.target_id for edge in outgoing_edges(flow_ir, node_id)}
