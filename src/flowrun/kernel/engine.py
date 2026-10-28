"""FlowRun 域D2 · 执行内核主循环（tasks.md T010，design.md §5）。

``run_flow`` 是内核唯一公共入口：驱动 FlowIR 直到等待点（人审 / 闸 / 暂停 /
全部完成），返回收口时的 FlowState。真相只在 ledger——每轮从
``recover_flow_state`` 恢复状态推进（快照 + 增量重放，与全量重放等价），
因此同一工作区反复调用 run_flow 与一次连续执行语义一致（崩溃/暂停续跑）。

进入主循环前做一次**工作区身份校验**（FIX-B3 / K2，公开函数
``verify_workspace_matches_flow``）：账本非空时，其 flow_started 的 run 名与
计划节点集合必须与当前 IR **严格集合相等**（多出或缺失都拒绝），否则抛
``FlowWorkspaceMismatchError``（CLI 捕获后退 4）——防止
``flowrun run b.mmd --workspace a的工作区`` 恢复出 A 的 flow_done 后立刻宣告
完成、把 B 整图静默跳过，也防止"新图追加节点"静默继承旧 run 的 done。
FAIL-CLOSED：**一切"无法校验身份"的情形都拒绝**而非放行——账本非空却
缺 flow_started、flow_started 的 planned_nodes 缺失或为空，都与集合不等价
同等对待（同样抛异常、退 4、零写入），旧实现这两处直接跳过即 fail-open 漏洞。

主循环每轮（任务书 T010 冻结顺序；②③ 于 FIX-B2 换序）：
① 恢复 FlowState；running 节点视为上次进程中断 → 落 node_failed
   (exit_code=None) 后走其后继处置（重试/闸）——不静默重跑：重投语义由
   attempt 递增的幂等键显式承载（docstring 取舍：崩溃点产物完整性未知，
   宁可转人审也不掩盖一次真实失败）；
② 等待点：全局 paused / halted / done / 存在 awaiting_human → 写 todo 返回。
   FIX-B2：等待点先于机器闸——已暂停的流再 run 不产生任何新事件，闸因此
   不会被反复触发刷账；
③ 机器闸（hop / budget）：命中即 gate_tripped 落账并暂停返回。闸语义为
   **每工作区每闸名至多一次**，resume = 人审对既有超限的永久豁免
   （见 engine_gates 模块 docstring）；
④ 推进 ready 节点（task 并发，见 engine_node_executors）；
⑤ finished 节点出边激活：done/skipped 的无条件边与 barrier 边 → edge_taken
   （跨轮幂等：同 source→target 只激活一次；target 已是终态 done/skipped/failed
   的边跳过且不落 edge_taken——**投影只前进不后退**，FIX-B1 + K3）；failed 节点
   按 engine_gates 的失败处置策略走超时分支 / 重新入队（node_entered） / retry 闸；
⑥ 快照（默认 SnapshotStore.maybe_snapshot，可用 hook 替换）；
⑦ 收口：全部节点 done/skipped → flow_done；收口前再查一次闸，防最后一跳
   烧穿预算仍宣告完成；整轮零进展（如 join 等前驱）→ 挂起返回不空转。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from flowrun.ir.schema import FlowIR, NodeSpec
from flowrun.state.flow_state import (
    STATUS_AWAITING_HUMAN,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_JUDGING,
    STATUS_PAUSED,
    STATUS_READY,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    FlowState,
    edge_taken_may_activate_target,
    is_finished_status,
)
from flowrun.state.ledger import EventLedger
from flowrun.state.snapshots import SnapshotStore, recover_flow_state

from .engine_gates import (
    FAILURE_ACTION_REQUEUE,
    FAILURE_ACTION_TIMEOUT_BRANCH,
    FAILURE_ACTION_TRIP_GATE,
    GATE_RETRY_EXHAUSTED,
    GateFinding,
    append_edge_taken,
    count_node_events,
    find_untripped_machine_gates,
    has_edge_been_taken,
    outgoing_activatable_edges,
    resolve_failed_node_policy,
    trip_gate,
)
from .engine_graph import first_timeout_edge
from .engine_node_executors import advance_ready_nodes
from .todo_projection import write_todo
from .workspace_identity import (
    FlowWorkspaceMismatchError,
    verify_workspace_matches_flow,
)
from .workspace_layout import (
    WorkspaceLayout,
    layout_for_new_run,
    layout_for_open_ledger,
)

__all__ = [
    "EngineHooks",
    "FlowWorkspaceMismatchError",
    "run_flow",
    "verify_workspace_matches_flow",
]


@dataclass
class EngineHooks:
    """内核的外部能力注入点（测试注 stub，生产由 cli 装配）。

    - build_task_packet(flow_ir, node, work_dir, packet_dir) -> Path
      （域D1 packet_builder 冻结签名）；
    - probe_agent(agent_name) -> bool（shutil.which 探测的封装）；
    - dispatch_task(node, packet_path, timeout_seconds) -> DispatchResult
      （awaitable；域C AgentAdapter 的封装）；
    - judge_decision(flow_ir, decision_node, evidence_pack, work_dir,
      packet_dir, timeout_seconds) -> JudgeVerdict（同步或 awaitable 均可，
      域D1 kernel.judge 冻结签名）；
    - snapshot(ledger, flow_state)（可选，缺省用 SnapshotStore.maybe_snapshot）。
    """

    build_task_packet: Callable[[FlowIR, NodeSpec, Path, Path], Path]
    probe_agent: Callable[[str], bool]
    dispatch_task: Callable[..., object]
    judge_decision: Callable[..., object]
    snapshot: Callable[[EventLedger, FlowState], object] | None = None


async def run_flow(
    flow_ir: FlowIR,
    workspace: Path,
    run_hooks: EngineHooks,
    ledger: EventLedger | None = None,
) -> FlowState:
    """驱动流程到等待点或完成，返回收口时的 FlowState（已写 todo 投影）。

    workspace 布局（T022，唯一真相 ``kernel.workspace_layout``）：
    - **legacy**：工作区根存在 ``ledger.jsonl`` → ``ledger.jsonl`` / ``snapshots/``
      / ``todo.md`` / ``packets/`` / ``audit/`` 全部在根（既有工作区零迁移）；
    - **namespaced**：根没有 ``ledger.jsonl`` → 本次 run 独占
      ``<workspace>/<run_name>/``，五类运行期资产都在该子目录内，各 run 互不串扰。
    借用账本时以账本所在目录为准（调用方可能已按子目录打开），不再二次推断。

    账本单实例约定：``ledger`` 传入则**借用**——全程只用该实例追加、**绝不 close**
    （由调用方负责生命周期）；为 None 才自建自管（构造并在收口/异常时关闭），
    保持默认行为与既有调用点零变化。
    """
    workspace = Path(workspace).resolve()
    # 布局唯一真相：写入路径按**本次 run** 定（namespaced = <ws>/<run>/），不依赖
    # "此刻根有没有账本"——否则"先建 run 目录、后落账本"的时序会把首轮误判成
    # legacy。借用账本时以账本自身所在目录为准（调用方可能已按子目录打开）。
    layout = _layout_for_borrowed_ledger(ledger, workspace, flow_ir.run_name)
    packet_dir = layout.packets_dir
    audit_dir = layout.audit_dir
    snapshots_dir = layout.snapshots_dir
    for required_dir in layout.required_directories():
        required_dir.mkdir(parents=True, exist_ok=True)
    owns_ledger = ledger is None
    if owns_ledger:
        ledger = EventLedger(layout.ledger_path)
    try:
        snapshot_store = SnapshotStore(snapshots_dir)
        snapshot_action = run_hooks.snapshot or snapshot_store.maybe_snapshot
        todo_path = layout.todo_path
        run_name = flow_ir.run_name
        dispatch_results: dict[str, object] = {}  # 本进程内各节点最近一次 DispatchResult

        def finish() -> FlowState:
            final_state = recover_flow_state(ledger, snapshot_store)
            write_todo(flow_ir, final_state, todo_path)
            if owns_ledger:
                ledger.close()  # 关闭追加句柄（幂等）；借用账本绝不关闭
            return final_state

        verify_workspace_matches_flow(flow_ir, ledger)

        if ledger.last_seq == 0:
            ledger.append(
                "flow_started",
                {
                    "entry_nodes": list(flow_ir.entry_node_ids),
                    "planned_nodes": list(flow_ir.nodes),
                },
                f"{run_name}-flow-started",
                run_name,
            )

        while True:
            events_before_turn = ledger.last_seq
            flow_state = recover_flow_state(ledger, snapshot_store)

            # ① 崩溃恢复：running = 上次进程中断（落 node_failed 后 continue，
            #    让下轮以 failed 身份走 ⑤ 的后继处置）
            interrupted_node_ids = [
                node_id
                for node_id in flow_ir.nodes
                if flow_state.node_status(node_id) == STATUS_RUNNING
            ]
            for node_id in interrupted_node_ids:
                failure_occurrence = count_node_events(ledger, "node_failed", node_id) + 1
                ledger.append(
                    "node_failed",
                    {"exit_code": None, "reason": "interrupted_by_restart"},
                    f"{run_name}-{node_id}-node-failed-{failure_occurrence}",
                    run_name,
                    node_id,
                )
            if interrupted_node_ids:
                continue

            # ② 等待点（FIX-B2：先于机器闸）：暂停 / 终止 / 完成 / 人审挂起。
            #    已暂停的流再 run 不产生任何新事件——闸不会因此重复刷账。
            if (
                flow_state.is_paused()
                or flow_state.is_halted()
                or flow_state.is_done()
                or flow_state.pending_human_nodes()
            ):
                return finish()

            # ③ 机器闸（hop / budget）：每工作区每闸名至多触发一次（FIX-B2）。
            #    K1：两个闸各自独立判定，未了结的命中**逐条**落账（trip_gate 自身按闸名幂等，
            #    一个闸的豁免不会连坐另一个闸）。
            machine_gate_findings = find_untripped_machine_gates(flow_ir, flow_state, ledger)
            if machine_gate_findings:
                for gate_finding in machine_gate_findings:
                    trip_gate(ledger, run_name, gate_finding)
                return finish()

            # ④ 推进 ready 节点（human_requested → 本轮挂起返回）
            if await advance_ready_nodes(
                flow_ir, flow_state, ledger, run_hooks, layout.run_root, dispatch_results
            ):
                return finish()

            # ⑤ finished 节点出边激活 + failed 节点处置
            activate_finished_nodes(flow_ir, recover_flow_state(ledger, snapshot_store), ledger)

            # ⑥ 快照（状态必须已投影到账本 head，故先恢复再快照）
            flow_state = recover_flow_state(ledger, snapshot_store)
            snapshot_action(ledger, flow_state)

            # ⑦ 收口：全部 resolved → flow_done；闸复查防"最后一跳烧穿预算"。
            #    K1：与 ③ 同口径——未了结的机器闸逐条落账（hop / budget 互不遮蔽）。
            all_nodes_resolved = all(
                is_finished_status(flow_state.node_status(node_id))
                for node_id in flow_ir.nodes
            )
            if all_nodes_resolved:
                final_gate_findings = find_untripped_machine_gates(flow_ir, flow_state, ledger)
                if final_gate_findings:
                    for gate_finding in final_gate_findings:
                        trip_gate(ledger, run_name, gate_finding)
                else:
                    ledger.append("flow_done", {}, f"{run_name}-flow-done", run_name)
                return finish()
            if ledger.last_seq == events_before_turn:
                # 零进展轮：无 ready/running/judging/awaiting/paused/failed 节点，
                # 则剩余 planned 节点已不可达（判定未选中的分支等）→ 判 flow_done；
                # 否则（如 join 等未齐的前驱）挂起返回，不空转。
                if not _has_pending_work(flow_ir, flow_state):
                    ledger.append("flow_done", {}, f"{run_name}-flow-done", run_name)
                return finish()
    finally:
        # 任何路径（正常 finish / 身份校验失败 / 主循环异常）都释放追加句柄，
        # 否则 Windows 下账本文件被占用，os.replace / 删除都会失败。
        # close() 幂等（finish 已关过则无操作），失败也不掩盖原始异常。
        # 借用账本（owns_ledger=False）绝不关闭——生命周期归调用方。
        if owns_ledger:
            try:
                ledger.close()
            except OSError:
                pass


def _layout_for_borrowed_ledger(
    ledger: EventLedger | None, workspace: Path, run_name: str
) -> WorkspaceLayout:
    """选定本次运行的布局。

    借账本 → 以账本所在目录为准（见 ``layout_for_open_ledger``）；不借 → 按
    ``flow_ir.run_name`` 走 namespaced 布局 ``<workspace>/<run_name>/``。

    内核的写入路径**永不现场推断"根有没有账本"**：``run_flow`` 是流程执行者，
    它只服务 ``flow_ir.run_name`` 这一个 run，路径确定性优先于兼容启发式。
    旧工作区的兼容由**调用方**完成（CLI 用 ``resolve_layout_for_read`` 解析后
    以借用账本方式传入），这样"同一个旧工作区继续扁平、新工作区按 run 分包"
    的判定只存在于一处，内核不重复判断。
    """
    if ledger is None:
        return layout_for_new_run(workspace, run_name)
    return layout_for_open_ledger(ledger.ledger_path, workspace, run_name)


def _has_pending_work(flow_ir: FlowIR, flow_state: FlowState) -> bool:
    """是否还有可能被推进的节点（planned 且不可达的节点不算待处理工作）。"""
    pending_statuses = frozenset(
        {
            STATUS_READY,
            STATUS_RUNNING,
            STATUS_JUDGING,
            STATUS_AWAITING_HUMAN,
            STATUS_PAUSED,
            STATUS_FAILED,
        }
    )
    return any(
        flow_state.node_status(node_id) in pending_statuses
        for node_id in flow_ir.nodes
    )


def activate_finished_nodes(
    flow_ir: FlowIR, flow_state: FlowState, ledger: EventLedger
) -> None:
    """⑤：对每个 done/skipped 节点激活其无条件边与 barrier 边（跨轮幂等）。

    边幂等键按**边身份**单射（``run-edge-<index>-<source>-><target>-<kind>``）：
    同源同目标的多条边各自独立，一条激活不影响另一条；同一条边重复激活才被
    幂等拦下（重试重跑同一节点后，其出边不会对同一目标二次投递——防自动边循环）。
    failed 节点交给 engine_gates 的处置策略：超时分支 / 重新入队 / retry 闸。
    skipped 节点视同 done 激活后继——"@skip 跳过该节点"意指绕过而非拦断
    全流（记事件不改图，design.md §5.1）。

    FIX-B1 + K3 第②层守卫（**投影只前进不后退**，单一真相 =
    ``flow_state.edge_taken_may_activate_target``）：target 已是终态
    （done / skipped / failed）时跳过该边激活且**不落 edge_taken**（边未被走，
    账本不得留下假事实）——fan-in 错峰收尾时晚到的 barrier 边不得把已收口的
    节点重新点燃；失败节点也不得被晚到入边退回 ready（它的重投只走
    ``node_entered``，见 ``_apply_failure_policy``）。
    """
    for node_id in flow_ir.nodes:
        node_status = flow_state.node_status(node_id)
        if node_status in (STATUS_DONE, STATUS_SKIPPED):
            for edge in outgoing_activatable_edges(flow_ir, node_id):
                if not edge_taken_may_activate_target(flow_state.node_status(edge.target_id)):
                    continue  # target 已终态：边不再激活（投影只前进不后退）
                if has_edge_been_taken(ledger, flow_ir, edge):
                    continue
                append_edge_taken(ledger, flow_ir, edge, edge.when)
        elif node_status == STATUS_FAILED:
            _apply_failure_policy(flow_ir, ledger, node_id)


def _apply_failure_policy(
    flow_ir: FlowIR, ledger: EventLedger, node_id: str
) -> None:
    """执行 engine_gates 给出的失败处置（超时分支 / 重入队 / retry_exhausted 闸）。

    retry_exhausted 闸同样受"每工作区每闸名至多一次"约束（FIX-B2）：同轮多个节点
    一起失败、或 resume 后反复 run，都只留一条 retry_exhausted 闸记录。
    """
    run_name = flow_ir.run_name
    failure_policy = resolve_failed_node_policy(flow_ir, ledger, node_id)
    if failure_policy.action == FAILURE_ACTION_TIMEOUT_BRANCH:
        timeout_edge = first_timeout_edge(flow_ir, node_id)
        if timeout_edge is not None and not has_edge_been_taken(ledger, flow_ir, timeout_edge):
            append_edge_taken(ledger, flow_ir, timeout_edge, None)
    elif failure_policy.action == FAILURE_ACTION_REQUEUE:
        requeue_occurrence = count_node_events(ledger, "node_entered", node_id) + 1
        ledger.append(
            "node_entered",
            {"reason": "retry", "failure_count": failure_policy.failures_so_far},
            f"{run_name}-{node_id}-requeue-{failure_policy.failures_so_far}-{requeue_occurrence}",
            run_name,
            node_id,
        )
    elif failure_policy.action == FAILURE_ACTION_TRIP_GATE:
        trip_gate(
            ledger, run_name,
            GateFinding(GATE_RETRY_EXHAUSTED, failure_policy.failures_so_far, failure_policy.retry_limit),
            node_id,
        )
