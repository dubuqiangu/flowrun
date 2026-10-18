"""FlowRun 域D2 · 工作区会话装配（CLI 各子命令复用的会话级操作）。

把 CLI 反复复用的会话级操作集中在此，避免每个子命令各自重复：

- ``open_workspace_state``：打开工作区账本并恢复 FlowState（快照 + 增量重放，
  等价全量重放）；
- ``workspace_ledger_path`` / ``workspace_is_initialized``：工作区账本路径与
  "是否已初始化"（以账本文件存在为唯一判据）；
- ``append_human_resolution``：approve/deny 通道的 human_resolved 事件生产者
  （幂等键带 occurrence 段，与 todo 通道同构）。

本模块从 ``flowrun.cli`` 原样拆出（纯搬移，签名与语义零变化）。
"""
from __future__ import annotations

from pathlib import Path

from flowrun.ir.schema import FlowIR
from flowrun.state.flow_state import FlowState
from flowrun.state.ledger import EventLedger
from flowrun.state.snapshots import SnapshotStore, recover_flow_state

__all__ = [
    "open_workspace_state",
    "workspace_ledger_path",
    "workspace_is_initialized",
    "append_human_resolution",
]


def open_workspace_state(workspace: Path) -> tuple[EventLedger, FlowState]:
    """打开工作区账本并恢复 FlowState（快照 + 增量重放，等价全量重放）。"""
    workspace = Path(workspace)
    ledger = EventLedger(workspace / "ledger.jsonl")
    snapshot_store = SnapshotStore(workspace / "snapshots")
    return ledger, recover_flow_state(ledger, snapshot_store)


def workspace_ledger_path(workspace: Path) -> Path:
    return Path(workspace) / "ledger.jsonl"


def workspace_is_initialized(workspace: Path) -> bool:
    """工作区是否已初始化（以账本文件存在为唯一判据）。"""
    return workspace_ledger_path(workspace).exists()


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
