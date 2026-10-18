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

出边幂等键（K3，按**单条边**唯一标识）：``run-edge-<index>-<source>-><target>-<kind>``。
旧实现只按 ``source->target`` 生成键，同一对节点间的多条边（when/retry/timeout/auto
并行）会共用一键而互相压制；新键以 ``index``（边在 FlowIR 中的位次）保证唯一。
**跨版本不兼容**：旧账本里的 ``release-login-edge-A001-X001`` 形式键与新格式不等值，
同工作区升级内核后已落账的边可能被重新激活——升级请换新工作区。

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

from flowrun.ir.schema import EdgeSpec, FlowIR
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
    "EDGE_KIND_WHEN",
    "EDGE_KIND_RETRY",
    "EDGE_KIND_TIMEOUT",
    "EDGE_KIND_AUTO",
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
    "append_node_failed",
    "resolve_failed_node_policy",
    "count_node_events",
    "latest_event_payload",
    "edge_kind",
    "edge_idem_key",
    "has_edge_been_taken",
    "append_edge_taken",
    "outgoing_activatable_edges",
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

# 边类别（幂等键的排障段）：与 engine_graph 的边分类同序判定。
EDGE_KIND_RETRY = "retry"      # retry > 0：源失败后重入队
EDGE_KIND_TIMEOUT = "timeout"  # when=None 且带定时器：超时分支边
EDGE_KIND_WHEN = "when"        # when 非空：判定谓词边 / join barrier 边
EDGE_KIND_AUTO = "auto"        # 无条件边：源 done 即自动激活


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


def append_node_failed(
    ledger: EventLedger, run_name: str, node_id: str, payload: dict
) -> None:
    """落 node_failed（幂等键带失败次数段，重试后的再次失败各自计一条）。

    task / decision 两类执行器共用的失败事件生产者（原在 engine_node_executors）。
    """
    failure_occurrence = count_node_events(ledger, "node_failed", node_id) + 1
    ledger.append(
        "node_failed",
        payload,
        f"{run_name}-{node_id}-node-failed-{failure_occurrence}",
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


def edge_kind(edge: EdgeSpec) -> str:
    """边的类别（幂等键的排障段）：retry > timeout > when > auto。"""
    if edge.retry > 0:
        return EDGE_KIND_RETRY
    if edge.when is None and edge.timeout_seconds is not None:
        return EDGE_KIND_TIMEOUT
    if edge.when is not None:
        return EDGE_KIND_WHEN
    return EDGE_KIND_AUTO


def edge_idem_key(flow_ir: FlowIR, edge: EdgeSpec) -> str:
    """单条边的幂等键——**唯一标识 FlowIR 里的一条边**（可读、可排障）。

    FlowIR 的边没有 id 字段，故用它在 ``flow_ir.edges`` 里的**声明序号**做稳定
    身份（按对象同一性定位，故端到端完全重复的两条边也各得其所）；序号在语义上
    是稳的——换图会被工作区身份校验拦下（verify_workspace_matches_flow）。
    再带上 source→target 与类别，使键自身就能读出是哪条边。格式：

    ``<run>-edge-<index>-<source>-><target>-<kind>``

    修复点：旧键 ``<run>-edge-<source>-<target>`` 不含边身份，同源同目标的多条
    边（如一条 when:all + 一条 timeout）会撞成同一键，第二条边被当成"重复投递"
    丢弃，账本少记一条边激活（审计失真）。**键格式变更 → 旧账本跨版本续跑时边
    幂等键不兼容**（旧键的 edge_taken 不再被认作"这条边已走过"）。

    复杂度（T020i 实测、本轮**不改**）：本函数经 ``_edge_position_in_flow`` 对
    ``flow_ir.edges`` 做一次 O(E) 对象同一性扫描；它被 ``has_edge_been_taken``
    （本身还有 ``ledger.entries()`` 的一次 O(N) 列表拷贝）在每条候选出边上调用，
    故边激活路径含 O(E²) 项。⑤ 的主循环另有**结构性**的 O(N·E)：
    ``outgoing_activatable_edges`` → ``outgoing_edges`` 对每个节点都线性扫一遍
    全部边。实测 ``activate_finished_nodes``：\"链 + 全部 done\"（目标皆终态，
    不落 edge_taken）50/49 边 0.065ms → 800/799 边 8.5ms（翻倍 ≈ 3.2~4.0 倍，
    二次项确认）；\"星 + 目标非终态\"（真落 edge_taken）则转为**每条边一次 fsync**
    主导（50 边 ≈ 109ms，其中 50 次落账 fsync ≈ 100ms）。**决策：保留**。理由：
    flow.mmd 是人写的，现实规模 ≤ 数十节点（整条热路径亚毫秒~百毫秒级）；给边身份
    加索引会引入第二处\"边身份\"真相（与 ``verify_workspace_matches_flow`` 的图身份
    契约耦合），风险大于收益。若将来出现自动生成的大图，再单独立项做索引化 +
    批量落账。
    """
    edge_index = _edge_position_in_flow(flow_ir, edge)
    return (
        f"{flow_ir.run_name}-edge-{edge_index}-"
        f"{edge.source_id}->{edge.target_id}-{edge_kind(edge)}"
    )


def _edge_position_in_flow(flow_ir: FlowIR, edge: EdgeSpec) -> int:
    """边在 ``flow_ir.edges`` 里的声明序号（按对象同一性，重复边也各有其位）。"""
    for edge_index, candidate in enumerate(flow_ir.edges):
        if candidate is edge:
            return edge_index
    raise ValueError("边不在该 FlowIR 的 edges 声明中，无法生成幂等键")


def has_edge_been_taken(ledger: EventLedger, flow_ir: FlowIR, edge: EdgeSpec) -> bool:
    """该条边是否已激活过——⑤ 跨轮幂等的唯一判据（幂等键按边身份单射）。

    同源同目标的多条边各自独立：一条已走过不影响另一条被激活。
    """
    return any(entry.idem == edge_idem_key(flow_ir, edge) for entry in ledger.entries())


def append_edge_taken(
    ledger: EventLedger,
    flow_ir: FlowIR,
    edge: EdgeSpec,
    when_text: str | None = None,
) -> LedgerEntry:
    """落一条 edge_taken 事件（source→done 审计、target→ready 投影）。"""
    payload = {
        "source": edge.source_id,
        "target": edge.target_id,
        "kind": edge_kind(edge),
    }
    if when_text is not None:
        payload["when"] = when_text
    return ledger.append(
        "edge_taken",
        payload,
        edge_idem_key(flow_ir, edge),
        flow_ir.run_name,
        edge.source_id,
    )


def outgoing_activatable_edges(flow_ir: FlowIR, node_id: str) -> list[EdgeSpec]:
    """done 节点待激活的出边：无条件边 + barrier 边（按声明顺序，含重复边）。

    barrier 边（when="all"）在源 done 时即可激活——等齐语义由 join 执行器
    在其 node_done 前把关，这里的激活只负责"让 join 变 ready"。
    超时分支边不在此列：它只在源节点超时失败时走（见 resolve_failed_node_policy）。
    """
    return [
        edge
        for edge in outgoing_edges(flow_ir, node_id)
        if (edge.when is None and edge.timeout_seconds is None) or edge.when == "all"
    ]
