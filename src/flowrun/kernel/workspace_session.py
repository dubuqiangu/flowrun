"""FlowRun 域D2 · 工作区会话装配（CLI 各子命令复用的会话级操作）。

把 CLI 反复复用的会话级操作集中在此，避免每个子命令各自重复：

- ``open_workspace_state``：打开**某个 run** 的账本并恢复 FlowState（快照 +
  增量重放，等价全量重放）；
- ``workspace_ledger_path`` / ``workspace_is_initialized``：某 run 的账本路径，
  与"该 run 是否已初始化"（以账本文件存在为唯一判据）；
- ``append_human_resolution``：approve/deny 通道的 human_resolved 事件生产者
  （幂等键带 occurrence 段，与 todo 通道同构）。

布局（T022）：路径一律经 ``kernel.workspace_layout`` 推导，不再在本模块里
拼 ``workspace / "ledger.jsonl"``。legacy 工作区（根有账本）拿到的是根路径，
行为与改造前逐字节一致；namespaced 工作区拿到的是 ``<workspace>/<run>/``。

本模块从 ``flowrun.cli`` 原样拆出（纯搬移，签名与语义零变化）；T022 起接受
``run_name``，缺省时按"根工作区"处理（legacy 语义，兼容既有调用点）。
"""
from __future__ import annotations

from pathlib import Path

from flowrun.ir.schema import FlowIR
from flowrun.state.flow_state import FlowState
from flowrun.state.ledger import EventLedger
from flowrun.state.snapshots import SnapshotStore, recover_flow_state

from .workspace_layout import resolve_layout_for_read

__all__ = [
    "open_workspace_state",
    "workspace_ledger_path",
    "workspace_is_initialized",
    "append_human_resolution",
]


def open_workspace_state(
    workspace: Path, run_name: str | None = None
) -> tuple[EventLedger, FlowState]:
    """打开该 run 的账本并恢复 FlowState（快照 + 增量重放，等价全量重放）。

    ``run_name`` 为 None 时按 legacy 语义只认工作区根账本（既有调用点零变化）；
    给出 run 名时先按布局解析位——已初始化的 legacy 工作区优先读根，
    否则读 ``<workspace>/<run>/``。
    """
    workspace_layout = resolve_layout_for_read(Path(workspace), run_name)
    ledger = EventLedger(workspace_layout.ledger_path)
    snapshot_store = SnapshotStore(workspace_layout.snapshots_dir)
    return ledger, recover_flow_state(ledger, snapshot_store)


def workspace_ledger_path(workspace: Path, run_name: str | None = None) -> Path:
    """该 run 的账本路径（只推导，不检查是否存在）。"""
    return resolve_layout_for_read(Path(workspace), run_name).ledger_path


def workspace_is_initialized(workspace: Path, run_name: str | None = None) -> bool:
    """该 run 是否已初始化（以账本文件存在为唯一判据）。

    未给 run 名时只看工作区根：这正是"这个工作区是否已被初始化"的既有语义
    （``run`` 冷启动判定与 CLI 退 5 判定都依赖它）。
    """
    return workspace_ledger_path(workspace, run_name).exists()


def append_human_resolution(
    flow_ir: FlowIR, ledger: EventLedger, node_id: str, decision: str
) -> None:
    """落一条 human_resolved 事件（幂等键带 occurrence 段，与 todo 通道同构）。"""
    prior_resolution_count = sum(
        1
        for entry in ledger.entries()
        if entry.type == "human_resolved" and entry.node == node_id
    )
    ledger.append(
        "human_resolved",
        {"decision": decision},
        f"{flow_ir.run_name}-{node_id}-human-resolved-{prior_resolution_count + 1}",
        flow_ir.run_name,
        node_id,
    )