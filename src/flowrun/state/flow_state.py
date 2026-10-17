"""FlowState —— ledger 事件的只读领域状态投影（FlowRun 域B，design.md §5.1 状态机）。

FlowState 不打开文件、不自己落盘：它由 `EventLedger.replay()` 全量投影产生，或由
`FlowState.from_dict()` + `merge_from_events()` 从快照增量恢复（见 state.snapshots.recover_flow_state）。

## 事件 → 状态投影规则（17 种事件类型；域B 只读下列 payload 字段，域D 按此落账）

| 事件类型 | payload 最小字段约定 | 状态影响 |
|---|---|---|
| flow_started | `entry_nodes`: list[str]；`planned_nodes`: list[str]（均可选） | entry_nodes → ready；planned_nodes → planned；event.node（若给）→ ready |
| node_entered | 无 | node → ready |
| agent_dispatched | 无 | node → running；hop_count +1 |
| agent_exited | `exit_code`（只审计，不解读） | 无状态影响（等 verdict 事件来转移） |
| verdict | `edge` / `confidence` / `evidence`（只审计，不解读） | node → judging |
| edge_taken | `target`: str（目标转移）；`source`: str（event.node 缺席时备用）；`edge`（只审计） | source → done；target → ready（target 已终态 done/failed/skipped 则**不动**——投影只前进不后退，FIX-B1 + K3） |
| node_done | `tokens`: 数字（可选，累加） | node → done |
| node_failed | 无 | node → failed |
| human_requested | 无 | node → awaiting_human |
| human_resolved | `decision`: "approve" 或 "deny" | approve → done；deny → failed；其他值 → 维持 awaiting_human |
| checkpoint | `snapshot_path`（只审计） | 无状态影响 |
| gate_tripped | `gate`: str（闸名）；`actual` / `limit`（只审计） | 全局暂停位置 True；gate 名按序记入 tripped_gates() |
| user_directive | `directive`: "pause" 或 "skip" | pause → 全局暂停 + node（若处于 planned/ready/running/judging）转 paused，记住暂停前状态，flow_resumed 时恢复；skip → node（若非终态）转 skipped |
| flow_paused | 无 | 全局暂停位置 True |
| flow_resumed | 无 | 全局暂停位置 False；被 pause 的节点恢复到暂停前状态 |
| flow_halted | 无 | is_halted() → True（终态） |
| flow_done | 无 | is_done() → True（终态） |

通用规则：
- 任何已投影事件的 payload 若含数字字段 `tokens`，累加进 token_spent()（bool 不算数字）。
- 未知事件类型：忽略不投影（向前兼容；域B 不解析上表之外的语义）。
- 幂等去重在本层做：idem 已见过的条目整条丢弃（含其 tokens / hop 计数），只认第一条。

## 节点状态取值（node_status 返回值）

planned | ready | running | judging | done | failed | awaiting_human | paused | skipped | unknown

unknown = 从未被任何事件提及；paused / skipped 是用户指令修饰态（只记事件不改图）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # 仅类型标注用，避免与 ledger.py 的再导出形成运行期循环导入
    from .ledger import LedgerEntry

# ---- 节点状态常量（design.md §5.1 状态机 + 横向修饰态） ----
STATUS_PLANNED = "planned"
STATUS_READY = "ready"
STATUS_RUNNING = "running"
STATUS_JUDGING = "judging"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_AWAITING_HUMAN = "awaiting_human"
STATUS_PAUSED = "paused"
STATUS_SKIPPED = "skipped"
STATUS_UNKNOWN = "unknown"

# user_directive pause 可覆盖的节点状态：非终态、非人审挂起
_PAUSABLE_STATUSES = frozenset({STATUS_PLANNED, STATUS_READY, STATUS_RUNNING, STATUS_JUDGING})
# 终态：done / failed / skipped —— 之后不再被 pause / skip / edge_taken 改写
_TERMINAL_STATUSES = frozenset({STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED})
# "已走完"的收口终态（FIX-B1）：全流完成判定的依据（done / skipped，不含 failed）
_FINISHED_STATUSES = frozenset({STATUS_DONE, STATUS_SKIPPED})


def is_finished_status(status: str) -> bool:
    """status 是否属于"已走完"的收口终态（done / skipped）——全流完成判定的依据。

    与 ``edge_taken_may_activate_target`` 区分：本判定回答"整图是否收口"，
    因此**不含 failed**（失败节点不构成完成）。
    """
    return status in _FINISHED_STATUSES


def edge_taken_may_activate_target(status: str) -> bool:
    """edge_taken（边缘激活）能否把 target 置为 ready；False = 不得改写。

    单一真相（FIX-B1 + K3）：**终态不可被边缘激活改写**，done / skipped /
    failed 三者皆然：

    - done / skipped：fan-in 错峰收尾、barrier 补激活等**晚到的边**不得把已收口
      的节点重新点燃（投影只前进不后退）；
    - failed：晚到的入边同样不得把已失败节点退回 ready——失败节点的重投只能由
      失败处置策略显式落的 ``node_entered`` 承载（engine ⑤ 的
      ``_apply_failure_policy``），否则会绕过重试次数上限与 retry 闸。

    投影层（``FlowState._apply_edge_taken``）与内核 ⑤ 层
    （``activate_finished_nodes``）共用本判定，保证两处口径一致。
    """
    return status not in _TERMINAL_STATUSES



class FlowState:
    """重放 ledger 事件产生的只读领域状态投影（查询方法即 design.md §5.1 状态机语义）。"""

    def __init__(self) -> None:
        self._node_statuses: dict[str, str] = {}
        self._pre_pause_statuses: dict[str, str] = {}  # node -> 暂停前状态（flow_resumed 恢复用）
        self._flow_paused = False
        self._flow_halted = False
        self._flow_done = False
        self._token_total = 0
        self._dispatch_count = 0
        self._tripped_gate_names: list[str] = []
        self._seen_idem_keys: set[str] = set()

    # ---------------------------------------------------------------- 事件投影

    def merge_from_events(self, ledger_entries: Iterable[LedgerEntry]) -> FlowState:
        """按序投影一批 ledger 条目（idem 幂等去重：已见过的 idem 整条丢弃）。

        快照增量恢复的既定接口：`FlowState.from_dict(快照 state) → merge_from_events(seq > 快照点的条目)`。
        去重依据 `_seen_idem_keys`，它随 to_dict/from_dict 一并持久化，因此跨快照边界的重复投递
        （第一次出现在快照点之前、重复出现在之后）同样能被正确去重——这是恢复与全量重放等价的关键。
        返回 self 便于链式调用。
        """
        for entry in ledger_entries:
            self.apply_event(entry)
        return self

    def apply_event(self, entry: LedgerEntry) -> bool:
        """投影单条事件；因 idem 重复被丢弃时返回 False，实际投影返回 True。"""
        if entry.idem in self._seen_idem_keys:
            return False
        self._seen_idem_keys.add(entry.idem)
        self._accumulate_tokens(entry.payload)
        if entry.type == "agent_dispatched":
            self._dispatch_count += 1
        self._project_event(entry)
        return True

    def _project_event(self, entry: LedgerEntry) -> None:
        event_type = entry.type
        if event_type == "flow_started":
            self._apply_flow_started(entry)
        elif event_type == "node_entered":
            self._set_node_status(entry.node, STATUS_READY)
        elif event_type == "agent_dispatched":
            self._set_node_status(entry.node, STATUS_RUNNING)
        elif event_type == "verdict":
            self._set_node_status(entry.node, STATUS_JUDGING)
        elif event_type == "edge_taken":
            self._apply_edge_taken(entry)
        elif event_type == "node_done":
            self._set_node_status(entry.node, STATUS_DONE)
        elif event_type == "node_failed":
            self._set_node_status(entry.node, STATUS_FAILED)
        elif event_type == "human_requested":
            self._set_node_status(entry.node, STATUS_AWAITING_HUMAN)
        elif event_type == "human_resolved":
            self._apply_human_resolved(entry)
        elif event_type == "gate_tripped":
            self._apply_gate_tripped(entry)
        elif event_type == "user_directive":
            self._apply_user_directive(entry)
        elif event_type == "flow_paused":
            self._flow_paused = True
        elif event_type == "flow_resumed":
            self._apply_flow_resumed()
        elif event_type == "flow_halted":
            self._flow_halted = True
        elif event_type == "flow_done":
            self._flow_done = True
        elif event_type in ("checkpoint", "agent_exited"):
            pass  # 纯审计事件，无状态影响
        else:
            pass  # 未知事件类型：忽略不投影（向前兼容，见模块 docstring 通用规则）

    def _apply_flow_started(self, entry: LedgerEntry) -> None:
        for planned_node in entry.payload.get("planned_nodes") or []:
            self._set_node_status(planned_node, STATUS_PLANNED)
        for entry_node in entry.payload.get("entry_nodes") or []:
            self._set_node_status(entry_node, STATUS_READY)
        if entry.node is not None:
            self._set_node_status(entry.node, STATUS_READY)

    def _apply_edge_taken(self, entry: LedgerEntry) -> None:
        source_node = entry.node if entry.node is not None else entry.payload.get("source")
        target_node = entry.payload.get("target")
        if source_node is not None:
            self._set_node_status(source_node, STATUS_DONE)
        if target_node is not None and edge_taken_may_activate_target(
            self.node_status(target_node)
        ):
            # FIX-B1 + K3 投影层守卫（投影只前进不后退）：已终态（done / skipped /
            # failed）的 target 不得被晚到的边退回 ready——否则下一轮会重复投递
            # （attempt=2、idem 不同、幂等去重拦不住），失败节点更会绕过重试闸。
            self._set_node_status(target_node, STATUS_READY)

    def _apply_human_resolved(self, entry: LedgerEntry) -> None:
        decision = entry.payload.get("decision")
        if decision == "approve":
            self._set_node_status(entry.node, STATUS_DONE)
        elif decision == "deny":
            self._set_node_status(entry.node, STATUS_FAILED)
        # 其他 decision 值：节点维持 awaiting_human（M1 只认 approve / deny）

    def _apply_gate_tripped(self, entry: LedgerEntry) -> None:
        gate_name = entry.payload.get("gate")
        if isinstance(gate_name, str) and gate_name:
            self._tripped_gate_names.append(gate_name)
        self._flow_paused = True  # 三闸动作统一为暂停转人审（design.md §5.3）

    def _apply_user_directive(self, entry: LedgerEntry) -> None:
        directive = entry.payload.get("directive")
        if directive == "pause":
            self._flow_paused = True
            if entry.node is not None:
                current_status = self._node_statuses.get(entry.node)
                if current_status in _PAUSABLE_STATUSES:
                    self._pre_pause_statuses[entry.node] = current_status
                    self._node_statuses[entry.node] = STATUS_PAUSED
        elif directive == "skip":
            if entry.node is not None and self._node_statuses.get(entry.node) not in _TERMINAL_STATUSES:
                self._set_node_status(entry.node, STATUS_SKIPPED)
        # 其他 directive 值（如 M2 的 approve）：M1 忽略，不改状态

    def _apply_flow_resumed(self) -> None:
        self._flow_paused = False
        for paused_node, previous_status in self._pre_pause_statuses.items():
            self._node_statuses[paused_node] = previous_status
        self._pre_pause_statuses.clear()

    def _set_node_status(self, node_id: str | None, status: str) -> None:
        if node_id is None:  # 事件未指定 node：状态无处理对象
            return
        self._node_statuses[node_id] = status
        if status != STATUS_PAUSED:
            self._pre_pause_statuses.pop(node_id, None)  # 已离开 paused，恢复记录不再有意义

    def _accumulate_tokens(self, payload: dict) -> None:
        token_amount = payload.get("tokens")
        if isinstance(token_amount, (int, float)) and not isinstance(token_amount, bool):
            self._token_total += int(token_amount)

    # ---------------------------------------------------------------- 查询方法

    def node_status(self, node_id: str) -> str:
        """节点状态：见模块 docstring 的 10 个取值；未被任何事件提及 → "unknown"。"""
        return self._node_statuses.get(node_id, STATUS_UNKNOWN)

    def completed_nodes(self) -> set[str]:
        """状态为 done 的节点 id 集合。"""
        return {node_id for node_id, status in self._node_statuses.items() if status == STATUS_DONE}

    def pending_human_nodes(self) -> set[str]:
        """挂起待人审（awaiting_human）的节点 id 集合。"""
        return {
            node_id
            for node_id, status in self._node_statuses.items()
            if status == STATUS_AWAITING_HUMAN
        }

    def is_paused(self) -> bool:
        """全局暂停位（flow_paused / gate_tripped / user_directive:pause 置位；flow_resumed 复位）。"""
        return self._flow_paused

    def is_halted(self) -> bool:
        """流已被 flow_halted 终止。"""
        return self._flow_halted

    def is_done(self) -> bool:
        """流已正常完成（flow_done）。"""
        return self._flow_done

    def token_spent(self) -> int:
        """累计 token 消耗 = 全部已投影事件 payload["tokens"] 数字之和。"""
        return self._token_total

    def hop_count(self) -> int:
        """跳数 = 已去重的 agent_dispatched 事件数（每次向 agent 投递算一跳，重试重投也计入）。"""
        return self._dispatch_count

    def tripped_gates(self) -> tuple[str, ...]:
        """按触发顺序记录的三闸名称（gate_tripped payload["gate"]）。"""
        return tuple(self._tripped_gate_names)

    # ---------------------------------------------------------------- 序列化

    def to_dict(self) -> dict:
        """完整序列化（含已见 idem 集合——快照恢复后继续去重的依据）。"""
        return {
            "node_statuses": dict(self._node_statuses),
            "pre_pause_statuses": dict(self._pre_pause_statuses),
            "flow_paused": self._flow_paused,
            "flow_halted": self._flow_halted,
            "flow_done": self._flow_done,
            "token_total": self._token_total,
            "dispatch_count": self._dispatch_count,
            "tripped_gate_names": list(self._tripped_gate_names),
            "seen_idem_keys": sorted(self._seen_idem_keys),
        }

    @classmethod
    def from_dict(cls, serialized_state: dict) -> FlowState:
        """从 to_dict 的产物恢复（缺字段按空值处理，容忍旧版快照）。"""
        flow_state = cls()
        flow_state._node_statuses = dict(serialized_state.get("node_statuses") or {})
        flow_state._pre_pause_statuses = dict(serialized_state.get("pre_pause_statuses") or {})
        flow_state._flow_paused = bool(serialized_state.get("flow_paused", False))
        flow_state._flow_halted = bool(serialized_state.get("flow_halted", False))
        flow_state._flow_done = bool(serialized_state.get("flow_done", False))
        flow_state._token_total = int(serialized_state.get("token_total", 0))
        flow_state._dispatch_count = int(serialized_state.get("dispatch_count", 0))
        flow_state._tripped_gate_names = list(serialized_state.get("tripped_gate_names") or [])
        flow_state._seen_idem_keys = set(serialized_state.get("seen_idem_keys") or [])
        return flow_state

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FlowState) and self.to_dict() == other.to_dict()

    def __repr__(self) -> str:
        return (
            f"FlowState(nodes={self._node_statuses!r}, paused={self._flow_paused}, "
            f"halted={self._flow_halted}, done={self._flow_done}, "
            f"tokens={self._token_total}, hops={self._dispatch_count})"
        )
