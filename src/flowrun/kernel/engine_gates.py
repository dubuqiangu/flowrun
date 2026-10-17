"""FlowRun 域D2 · 三道闸与失败处置策略（design.md §5.3，tasks.md T010）。

机器闸只负责"叫人"：任一闸触发统一落 ``gate_tripped`` 事件并暂停转人审
（FlowState 对 gate_tripped 的投影即置全局暂停位），内核不自行 halt。

闸清单：
- ``hop``：agent 投递跳数 > 4×节点数（防判定/重试构成的失控循环）；
- ``budget``：累计 token > frontmatter budget_tokens；
- ``retry``：失败节点重试耗尽（或根本没有重试边）；
- ``verdict``：判定器自报 tripped（低置信 / 证据不足）；
- ``illegal_edge``：verdict 选中的目标不在判定节点的出边集合内。

失败处置（⑤ 的 failed 分支，按优先级）：
1. 失败原因为超时且存在超时分支边 → 走超时分支（"定时器先到即走此边"）；
2. 存在重试边且失败次数未耗尽 → 重新入队重跑（node_entered，attempt 递增）；
3. 否则 → ``gate_tripped(gate="retry")`` 转人审。

计数与幂等：失败/闸的发生次数都从 ledger 事件现算（重放等价——
"ready 的唯一来源是 ledger 事件"），idem 幂等键带 occurrence 段，
同轮崩溃恢复后重复处置会因 FlowState 状态已变化而自然短路。

闸的一次性语义（FIX-B2，docstring 冻结）：**每工作区每闸名至多触发一次**。
``gate_has_been_settled`` 是唯一判据：账内已出现同名 ``gate_tripped`` 即视为
"了结"——无论其后是否跟过 ``flow_resumed``：

- 其后**没有** ``flow_resumed`` → 该闸正等人在审，不再刷账（同轮两个节点同时
  失败、或反复 run 同一工作区，都只留一条 ``gate_tripped``）；
- 其后**有** ``flow_resumed`` → 该闸视为被人审**永久豁免**，不再触发。
  resume 的语义就是"人审对既有超限的显式豁免"——机器闸已经叫过人、人也明确
  放行了，重复触发只会把流程钉死在原地（实测根因：resume 后再 run 立刻重新
  触发 hop/budget 闸，永远无法推进）。
"""

from __future__ import annotations

from dataclasses import dataclass

from flowrun.ir.schema import FlowIR
from flowrun.state.flow_state import FlowState
from flowrun.state.ledger import EventLedger, LedgerEntry

from .engine_graph import first_retry_edge, first_timeout_edge, outgoing_edges

__all__ = [
    "GATE_HOP",
    "GATE_BUDGET",
    "GATE_RETRY",
    "GATE_VERDICT",
    "GATE_ILLEGAL_EDGE",
    "HOP_LIMIT_PER_NODE",
    "GateFinding",
    "FailurePolicy",
    "FAILURE_ACTION_TIMEOUT_BRANCH",
    "FAILURE_ACTION_REQUEUE",
    "FAILURE_ACTION_TRIP_GATE",
    "hop_limit",
    "check_cycle_gates",
    "find_untripped_machine_gates",
    "gate_has_been_settled",
    "append_gate_tripped",
    "trip_gate",
    "resolve_failed_node_policy",
    "count_node_events",
    "latest_event_payload",
]

GATE_HOP = "hop"
GATE_BUDGET = "budget"
GATE_RETRY = "retry"
GATE_VERDICT = "verdict"
GATE_ILLEGAL_EDGE = "illegal_edge"

HOP_LIMIT_PER_NODE = 4  # hop ≤ 4×节点数（design.md §5.3）

FAILURE_ACTION_TIMEOUT_BRANCH = "timeout_branch"
FAILURE_ACTION_REQUEUE = "requeue"
FAILURE_ACTION_TRIP_GATE = "trip_gate"


@dataclass
class GateFinding:
    """一次闸命中的结构化描述（actual/limit 只做审计落账）。"""

    gate: str
    actual: object
    limit: object


@dataclass
class FailurePolicy:
    """失败节点的处置决定（由 resolve_failed_node_policy 产出）。"""

    action: str  # FAILURE_ACTION_* 之一
    failures_so_far: int
    retry_limit: int
    timeout_target_id: str | None = None  # action=timeout_branch 时的分支目标


def hop_limit(flow_ir: FlowIR) -> int:
    """hop 闸上限 = 4×节点数。"""
    return HOP_LIMIT_PER_NODE * len(flow_ir.nodes)


def check_cycle_gates(flow_ir: FlowIR, flow_state: FlowState) -> list[GateFinding]:
    """检查每轮常开的机器闸（hop / budget），返回**全部**命中的有序列表。

    两个闸各自独立判定、互不遮蔽：hop 与 budget 可以同一轮同时命中，返回顺序
    固定为 hop 先、budget 后；都不命中时返回空列表。

    （修复 K1：旧实现用早退只返回一个命中闸，hop 优先会把 budget 永久遮蔽——
    hop 一旦永久超限并被 resume 豁免，budget 超限就再也不会被看到。）

    调用时机：主循环轮首（③）与收口前（⑦）——后者防"最后一跳烧穿预算
    仍宣告 flow_done"的窗口。
    """
    gate_findings: list[GateFinding] = []
    hop_actual = flow_state.hop_count()
    hop_ceiling = hop_limit(flow_ir)
    if hop_actual > hop_ceiling:
        gate_findings.append(GateFinding(GATE_HOP, hop_actual, hop_ceiling))
    budget_actual = flow_state.token_spent()
    if budget_actual > flow_ir.budget_tokens:
        gate_findings.append(GateFinding(GATE_BUDGET, budget_actual, flow_ir.budget_tokens))
    return gate_findings


def find_untripped_machine_gates(
    flow_ir: FlowIR, flow_state: FlowState, ledger: EventLedger
) -> list[GateFinding]:
    """机器闸（hop / budget）本轮该落账的命中列表（纯查询，不落账）。

    先取 ``check_cycle_gates`` 的全部命中，再**逐个按闸名**过滤已了结者
    （``gate_has_been_settled``：每工作区每闸名至多一次 + 其后有 flow_resumed
    即永久豁免）。逐个过滤是关键——一个闸被人审豁免不得连坐另一个闸。
    """
    return [
        gate_finding
        for gate_finding in check_cycle_gates(flow_ir, flow_state)
        if not gate_has_been_settled(ledger, gate_finding.gate)
    ]


def gate_has_been_settled(ledger: EventLedger, gate_name: str) -> bool:
    """该闸名是否已"了结"（账内已有同名 gate_tripped）。

    了结的两种形态都收敛为同一结论"不再触发"：
    - 其后没有 flow_resumed → 闸已触发、正等人在审，不重复刷账；
    - 其后出现过 flow_resumed → 人审对既有超限的显式豁免（永久豁免）。
    故判定只需按闸名查一次，无需区分二者（区分语义见模块 docstring）。
    """
    return any(
        entry.type == "gate_tripped" and entry.payload.get("gate") == gate_name
        for entry in ledger.entries()
    )


def trip_gate(
    ledger: EventLedger,
    run_name: str,
    gate_finding: GateFinding,
    node_id: str | None = None,
) -> LedgerEntry | None:
    """按一次性语义落 gate_tripped：闸已了结则返回 None（不落账）。

    全部闸出口（机器闸 hop/budget、失败处置 retry、判定 verdict/illegal_edge）
    都经此处，保证 ``tripped_gates()`` 永不出现同名重复。
    """
    if gate_has_been_settled(ledger, gate_finding.gate):
        return None
    return append_gate_tripped(ledger, run_name, gate_finding, node_id)


def append_gate_tripped(
    ledger: EventLedger,
    run_name: str,
    gate_finding: GateFinding,
    node_id: str | None = None,
) -> LedgerEntry:
    """落一条 gate_tripped 事件（idem 带同闸 occurrence 段，重复触发可追溯）。"""
    gate_occurrence = sum(
        1
        for entry in ledger.entries()
        if entry.type == "gate_tripped"
        and entry.payload.get("gate") == gate_finding.gate
    )
    return ledger.append(
        "gate_tripped",
        {
            "gate": gate_finding.gate,
            "actual": gate_finding.actual,
            "limit": gate_finding.limit,
        },
        f"{run_name}-gate-{gate_finding.gate}-{gate_occurrence + 1}",
        run_name,
        node_id,
    )


def resolve_failed_node_policy(
    flow_ir: FlowIR, ledger: EventLedger, node_id: str
) -> FailurePolicy:
    """对 failed 节点给出处置策略（纯查询，不落账——由 ⑤ 执行）。

    超时优先：最近一次 agent_exited 若标记 timed_out 且存在超时分支边，
    走分支；否则看重试边（retry:N = 失败后可重跑 N 次）；
    failures_so_far ≤ retry_limit → requeue，耗尽（或无重试边）→ 转人审。
    """
    failures_so_far = count_node_events(ledger, "node_failed", node_id)
    latest_exit_payload = latest_event_payload(ledger, "agent_exited", node_id)
    timed_out = bool((latest_exit_payload or {}).get("timed_out"))

    if timed_out:
        timeout_edge = first_timeout_edge(flow_ir, node_id)
        if timeout_edge is not None:
            return FailurePolicy(
                action=FAILURE_ACTION_TIMEOUT_BRANCH,
                failures_so_far=failures_so_far,
                retry_limit=0,
                timeout_target_id=timeout_edge.target_id,
            )

    retry_edge = first_retry_edge(flow_ir, node_id)
    retry_limit = retry_edge.retry if retry_edge is not None else 0
    if 1 <= failures_so_far <= retry_limit:
        # 只认真实 node_failed 计数：deny 等无失败计数的 failed 节点不得重入队
        return FailurePolicy(
            action=FAILURE_ACTION_REQUEUE,
            failures_so_far=failures_so_far,
            retry_limit=retry_limit,
        )
    return FailurePolicy(
        action=FAILURE_ACTION_TRIP_GATE,
        failures_so_far=failures_so_far,
        retry_limit=retry_limit,
    )


# ---------------------------------------------------------------- 计数查询


def count_node_events(
    ledger: EventLedger, event_type: str, node_id: str
) -> int:
    """某节点某类型事件的既有出现次数（attempt/occurrence 段与闸审计的依据）。"""
    return sum(
        1
        for entry in ledger.entries()
        if entry.type == event_type and entry.node == node_id
    )


def latest_event_payload(
    ledger: EventLedger, event_type: str, node_id: str
) -> dict | None:
    """某节点某类型事件最近一次的 payload（无则 None）。"""
    for entry in reversed(ledger.entries()):
        if entry.type == event_type and entry.node == node_id:
            return entry.payload
    return None


def has_edge_been_taken(ledger: EventLedger, run_name: str, source_id: str, target_id: str) -> bool:
    """(source,target) 边是否已激活过——⑤ 跨轮幂等的唯一判据。

    边幂等键单射：同一对 source→target 的无条件/barrier 边只激活一次；
    判定节点选边与 ⑤ 共用该键，因此"判定选中 + 出边激活"不会双落。
    """
    edge_idem_key = f"{run_name}-edge-{source_id}-{target_id}"
    return any(entry.idem == edge_idem_key for entry in ledger.entries())


def append_edge_taken(
    ledger: EventLedger,
    run_name: str,
    source_id: str,
    target_id: str,
    when_text: str | None = None,
) -> LedgerEntry:
    """落一条 edge_taken 事件（source→done 审计、target→ready 投影）。"""
    payload = {"source": source_id, "target": target_id}
    if when_text is not None:
        payload["when"] = when_text
    return ledger.append(
        "edge_taken",
        payload,
        f"{run_name}-edge-{source_id}-{target_id}",
        run_name,
        source_id,
    )


def outgoing_unconditional_targets(flow_ir: FlowIR, node_id: str) -> list[tuple[str, str | None]]:
    """done 节点待激活的出边 (target_id, when) 列表：无条件边 + barrier 边。

    barrier 边（when="all"）在源 done 时即可激活——等齐语义由 join 执行器
    在其 node_done 前把关，这里的激活只负责"让 join 变 ready"。
    超时分支边不在此列：它只在源节点超时失败时走（见 resolve_failed_node_policy）。
    """
    activated: list[tuple[str, str | None]] = []
    for edge in outgoing_edges(flow_ir, node_id):
        if edge.when is None and edge.timeout_seconds is None:
            activated.append((edge.target_id, None))
        elif edge.when == "all":
            activated.append((edge.target_id, edge.when))
    return activated
