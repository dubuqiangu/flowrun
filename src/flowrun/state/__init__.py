"""状态层（域B）：append-only 事件账本（ledger）+ 周期快照（snapshots）。

log 是真相，快照是投影。公开面见 state.ledger / state.flow_state / state.snapshots。
"""
