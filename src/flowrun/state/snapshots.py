"""FlowRun 域B · 快照与恢复（tasks.md T005，接口冻结见 interfaces.md §3）。

log 是真相，快照是投影：每 every_n_events 条事件落一份快照——FlowState 序列化 +
覆盖到的 seq + 覆盖点的 head_hash。恢复 = 最新快照 + ledger 增量重放，由本模块的
`recover_flow_state()` 一步完成（增量接口 = FlowState.merge_from_events，见其 docstring）。

写入原子性：先写同目录 temp 文件（flush + fsync），再 os.replace 原子替换；
崩溃留下的半成品只可能是 temp 文件，正式快照名永远完整可解析。

快照文件（canonical JSON，文件名 snapshot_<seq 十位零填>.json）：
{"created_ts":169...,"head_hash":"<覆盖点条目哈希>","seq":40,"state":{FlowState.to_dict()}}

load_latest() 只认 seq 最大的可解析快照；个别损坏文件跳过（退回更旧的快照只是多重放
几条事件，语义不变）。head_hash 用于恢复时核对"快照覆盖范围内账本未被篡改"。

K5 热路径：`recover_flow_state` 每轮被调用多次——链校验改为**增量**（见
`EventLedger.verify_chain`），最新快照记录做**进程内缓存**（失效判据 = 快照目录
条目数 / 最大文件名，见 `SnapshotStore._snapshot_directory_key`）。两者都只减少
重算次数，不改变恢复结果（磁盘格式与向后兼容性不变）。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from .flow_state import FlowState
from .ledger import EventLedger, LedgerCorruptionError, canonical_json, compute_entry_hash

__all__ = ["SnapshotStore", "recover_flow_state"]

_SNAPSHOT_FILE_PREFIX = "snapshot_"
_SNAPSHOT_FILE_SUFFIX = ".json"
_SNAPSHOT_TEMP_PREFIX = ".tmp_snapshot_"
# 扫描到的最新记录里额外挂的源文件路径（仅进程内使用，不落盘），供"记录形状非法"
# 的损坏诊断指出该检查哪个文件；下划线前缀标识它是内部元数据而非快照字段。
_SNAPSHOT_SOURCE_PATH_FIELD = "_source_path"


def _snapshot_state_or_raise(snapshot_record: dict) -> dict:
    """取快照记录的 ``state``；形状非法（缺失或非 dict）→ LedgerCorruptionError。

    只收敛"记录形状非法"这一类：把裸 ``KeyError('state')`` 变成带快照文件路径与 seq
    的可读损坏信号（CLI 退 6）。**不**捕获其它异常类型——宽范围吞掉 KeyError/TypeError/
    ValueError 会把无关的程序缺陷误诊为账本损坏。
    """
    serialized_state = snapshot_record.get("state")
    if isinstance(serialized_state, dict):
        return serialized_state
    source_path = snapshot_record.get(_SNAPSHOT_SOURCE_PATH_FIELD, "未知快照文件")
    snapshot_seq = snapshot_record.get("seq", "未知")
    raise LedgerCorruptionError(
        f"快照记录形状非法（缺 state 或非对象）：{source_path}（seq={snapshot_seq}）"
        "——请检查该快照文件或改用新工作区"
    )


class SnapshotStore:
    """快照仓库：周期落快照 + 读取最新快照（冻结 API 见 interfaces.md §3）。

    K5：最新快照记录在进程内缓存，失效判据是快照目录的（正式快照条目数,
    字典序最大文件名）——目录没变化就不重复 glob + 解析全部快照文件。缓存只
    影响"重算次数"，不影响读到的内容（新快照落盘必然改变目录条目数 / 最大
    文件名，缓存随即失效）。
    """

    def __init__(self, snapshot_dir: Path, every_n_events: int = 20) -> None:
        if every_n_events < 1:
            raise ValueError("every_n_events 必须是正整数")
        self.snapshot_dir = Path(snapshot_dir)
        self.every_n_events = every_n_events
        self._latest_record_cache: dict | None = None
        self._latest_record_cache_key: tuple[int, str] | None = None

    def maybe_snapshot(self, ledger: EventLedger, flow_state: FlowState) -> Path | None:
        """账本长度命中快照周期则落一份快照，返回快照路径；否则返回 None。

        触发条件：last_seq 非 0、是 every_n_events 的整倍数、且与已有最新快照的 seq 不同
        （防止在同一条目上重复落快照）。flow_state 必须已投影到 ledger 当前 head——
        由调用方（执行内核）保证，本方法不重放核对。
        """
        last_seq = ledger.last_seq
        if last_seq == 0:
            return None
        if last_seq % self.every_n_events != 0:
            return None
        latest_record = self._load_latest_record()
        if latest_record is not None and latest_record["seq"] == last_seq:
            return None
        snapshot_record = {
            "seq": last_seq,
            "head_hash": ledger.head_hash,
            "created_ts": time.time(),
            "state": flow_state.to_dict(),
        }
        return self._write_snapshot_record(snapshot_record)

    def load_latest(self) -> tuple[FlowState, int] | None:
        """最新可解析快照 → (状态, 已覆盖的 seq)；一个快照都没有则返回 None。"""
        latest_record = self._load_latest_record()
        if latest_record is None:
            return None
        return (
            FlowState.from_dict(_snapshot_state_or_raise(latest_record)),
            int(latest_record["seq"]),
        )

    # ---------------------------------------------------------------- 内部实现

    def _snapshot_file_path(self, snapshot_seq: int) -> Path:
        return self.snapshot_dir / f"{_SNAPSHOT_FILE_PREFIX}{snapshot_seq:010d}{_SNAPSHOT_FILE_SUFFIX}"

    def _write_snapshot_record(self, snapshot_record: dict) -> Path:
        """temp 文件 + fsync + os.replace 原子落盘，返回正式快照路径。"""
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        temp_file_descriptor, temp_file_path_str = tempfile.mkstemp(
            dir=self.snapshot_dir, prefix=".tmp_snapshot_", suffix=".json"
        )
        temp_file_path = Path(temp_file_path_str)
        final_path = self._snapshot_file_path(int(snapshot_record["seq"]))
        try:
            with os.fdopen(temp_file_descriptor, "wb") as temp_file_handle:
                temp_file_handle.write(canonical_json(snapshot_record).encode("utf-8"))
                temp_file_handle.flush()
                os.fsync(temp_file_handle.fileno())
            os.replace(temp_file_path, final_path)
        except BaseException:
            temp_file_path.unlink(missing_ok=True)  # 写失败清掉 temp，不留半成品
            raise
        return final_path

    def _load_latest_record(self) -> dict | None:
        """最新快照原始记录（进程内缓存；目录条目数 / 最大文件名变化才重扫）。"""
        directory_key = self._snapshot_directory_key()
        if directory_key != self._latest_record_cache_key:
            self._latest_record_cache = self._scan_latest_record()
            self._latest_record_cache_key = directory_key
        cached_record = self._latest_record_cache
        return dict(cached_record) if cached_record is not None else None

    def _snapshot_directory_key(self) -> tuple[int, str]:
        """缓存失效判据：(正式快照条目数, 字典序最大文件名)。

        文件名是 snapshot_<seq 十位零填>.json，故字典序最大名即 seq 最大的快照；
        新快照落盘必然让该键变化。只读目录名，不解析文件内容、不 stat。
        """
        if not self.snapshot_dir.exists():
            return (0, "")
        snapshot_file_names = [
            directory_entry.name
            for directory_entry in os.scandir(self.snapshot_dir)
            if directory_entry.name.startswith(_SNAPSHOT_FILE_PREFIX)
            and directory_entry.name.endswith(_SNAPSHOT_FILE_SUFFIX)
            and not directory_entry.name.startswith(_SNAPSHOT_TEMP_PREFIX)
        ]
        return (len(snapshot_file_names), max(snapshot_file_names, default=""))

    def _scan_latest_record(self) -> dict | None:
        """扫描目录取 seq 最大的可解析快照原始记录；损坏文件跳过（退回旧快照语义不变）。"""
        if not self.snapshot_dir.exists():
            return None
        candidate_records = []
        for snapshot_file in self.snapshot_dir.glob(f"{_SNAPSHOT_FILE_PREFIX}*{_SNAPSHOT_FILE_SUFFIX}"):
            if snapshot_file.name.startswith(_SNAPSHOT_TEMP_PREFIX):
                continue  # 崩溃残留的 temp 半成品，不是正式快照
            try:
                parsed_record = json.loads(snapshot_file.read_bytes().decode("utf-8"))
                if not isinstance(parsed_record, dict):
                    continue
                parsed_record[_SNAPSHOT_SOURCE_PATH_FIELD] = str(snapshot_file)
                candidate_records.append((int(parsed_record["seq"]), parsed_record))
            except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError):
                continue  # 单个坏文件不影响恢复：跳过它，用更旧的快照多重放几条
        if not candidate_records:
            return None
        return max(candidate_records, key=lambda seq_and_record: seq_and_record[0])[1]


def recover_flow_state(ledger: EventLedger, snapshot_store: SnapshotStore) -> FlowState:
    """恢复：最新快照 + ledger 增量重放（seq > 快照点），结果与全量 replay() 等价。

    无任何快照时退化为全量 ledger.replay()。增量机制：FlowState.from_dict(快照 state)
    → merge_from_events(seq > 快照点的条目)；快照持久化了已见 idem 集合，因此跨越快照
    边界的重复投递同样被去重。

    防篡改双重核对，任何一项失败都拒绝恢复（抛 LedgerCorruptionError）：
    1. 整链校验 ledger.verify_chain()（K5 起为该实例内的**增量**校验：已确认前缀
       不重复读盘，只校验新增条目；新实例首次调用仍是全量）——发现覆盖范围内任意
       一条被改动（含 seq 断裂）；
    2. 快照记录的 head_hash 与账本中 seq 恰为快照点的那条重算哈希一致——防御"整链
       重算哈希的自洽伪造"（快照在账本之外，攻击者无法回改快照里存的头哈希）。
    """
    if not ledger.verify_chain():
        raise LedgerCorruptionError("ledger 哈希链校验失败，拒绝基于被篡改的账本恢复")
    latest_record = snapshot_store._load_latest_record()
    if latest_record is None:
        return ledger.replay()
    snapshot_seq = int(latest_record["seq"])
    snapshot_head_hash = str(latest_record.get("head_hash", ""))
    ledger_entries = ledger.entries()
    entry_at_snapshot_seq = next(
        (entry for entry in ledger_entries if entry.seq == snapshot_seq), None
    )
    if entry_at_snapshot_seq is None:
        raise LedgerCorruptionError(
            f"账本中找不到快照覆盖的 seq={snapshot_seq}（账本在快照点之前被截断？）"
        )
    if snapshot_head_hash and compute_entry_hash(entry_at_snapshot_seq) != snapshot_head_hash:
        raise LedgerCorruptionError(
            f"快照点 seq={snapshot_seq} 的 head_hash 与账本不符：账本在快照覆盖范围内被篡改"
        )
    recovered_state = FlowState.from_dict(_snapshot_state_or_raise(latest_record))
    incremental_entries = [entry for entry in ledger_entries if entry.seq > snapshot_seq]
    return recovered_state.merge_from_events(incremental_entries)
