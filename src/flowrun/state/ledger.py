"""FlowRun 域B · 事件账本（tasks.md T004，接口冻结见 interfaces.md §3）。

append-only JSONL：每行一个事件的 canonical JSON。写路径 flush + fsync 落盘。

安全性设计：
- prev-link 哈希链：条目哈希 = sha256(prev_hash + canonical_json(条目去掉 prev_hash 字段))；
  下一条的 prev_hash 存上一条的哈希；首条 prev_hash = GENESIS_PREV_HASH（64 个 '0'）。
  verify_chain() 逐行重算比对（含 seq 连续性），并在实例内**增量**推进：已确认
  的前缀不重复读盘，只校验新增条目（K5）。已知性质：链只能发现"其后还有条目"
  的篡改——最后一条自身被改要等到下一条落账、或由快照记录的 head_hash 兜底
  （见 state.snapshots.recover_flow_state）。
- idem 幂等：append() 不去重（崩溃后允许同 idem 重复落账），replay()/FlowState 投影时
  相同 idem 只认第一条。
- 崩溃容错：打开账本时若文件末尾有未写完的行（无结尾换行符），直接截断丢弃——
  单写者假设下安全，避免后续追加把半行变成"中段垃圾"。

磁盘行格式（键与 LedgerEntry 字段一致；值即 canonical_json 的输出）：
{"idem":"...","node":"T001"|null,"payload":{...},"prev_hash":"<64hex>","run":"...","seq":1,"ts":1697...,"type":"..."}

注：design.md §4.4 示例把链字段写成 "prev"，interfaces.md（冻结契约）的数据类字段是
"prev_hash"，本模块按 interfaces.md 用 "prev_hash"，差异已列入交付报告。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from .flow_state import FlowState

__all__ = [
    "GENESIS_PREV_HASH",
    "LedgerCorruptionError",
    "LedgerEntry",
    "EventLedger",
    "FlowState",
    "canonical_json",
    "compute_entry_hash",
]

GENESIS_PREV_HASH = "0" * 64  # 首条 prev_hash：固定 64 位 '0'，保持链字段长度一致


class LedgerCorruptionError(Exception):
    """ledger/快照结构损坏（中段行不可解析、快照头不符、账本被截断等）。"""


def canonical_json(record: dict) -> str:
    """契约规定的规范 JSON：json.dumps(sort_keys=True, separators=(",", ":"))。"""
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


@dataclass
class LedgerEntry:
    """一条已落账事件（字段冻结见 interfaces.md §3）。"""

    seq: int
    prev_hash: str
    idem: str
    type: str
    run: str
    node: str | None
    ts: float
    payload: dict

    def to_dict(self) -> dict:
        """含 prev_hash 的完整磁盘形态（payload 为副本，防调用方借返回值篡改条目）。"""
        return {
            "seq": self.seq,
            "prev_hash": self.prev_hash,
            "idem": self.idem,
            "type": self.type,
            "run": self.run,
            "node": self.node,
            "ts": self.ts,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, record: dict) -> LedgerEntry:
        """从磁盘 JSON 恢复；缺键/类型不符抛 KeyError / TypeError / ValueError。"""
        return cls(
            seq=int(record["seq"]),
            prev_hash=str(record["prev_hash"]),
            idem=str(record["idem"]),
            type=str(record["type"]),
            run=str(record["run"]),
            node=str(record["node"]) if record["node"] is not None else None,
            ts=float(record["ts"]),
            payload=dict(record["payload"]),
        )


def compute_entry_hash(entry: LedgerEntry) -> str:
    """条目哈希 = sha256(prev_hash + canonical_json(条目去掉 prev_hash 字段)) 的 hex。

    契约来源 interfaces.md §3；该哈希即"下一条的 prev_hash 应存的值"。
    """
    entry_content_without_hash = {
        "seq": entry.seq,
        "idem": entry.idem,
        "type": entry.type,
        "run": entry.run,
        "node": entry.node,
        "ts": entry.ts,
        "payload": entry.payload,
    }
    hash_input = entry.prev_hash + canonical_json(entry_content_without_hash)
    return hashlib.sha256(hash_input.encode("utf-8")).hexdigest()


class EventLedger:
    """事件账本：JSONL 追加写 + 哈希链 + 幂等重放（冻结 API 见 interfaces.md §3）。

    追加语义：seq 全局递增；prev_hash 链到上一条；不查询去重（去重发生在投影层）。
    崩溃语义：构造时截断不完整尾行；追加后 flush + fsync 立即落盘。
    """

    def __init__(self, ledger_path: Path) -> None:
        self.ledger_path = Path(ledger_path)
        self._write_handle: IO[bytes] | None = None
        self._entries_cache: list[LedgerEntry] = []
        self._head_hash = GENESIS_PREV_HASH
        # 增量链校验游标（K5）：本实例已确认的账本前缀边界
        self._confirmed_entry_count = 0
        self._confirmed_head_hash = GENESIS_PREV_HASH
        self._confirmed_byte_offset = 0
        self._truncate_incomplete_tail_line()
        self._load_existing_entries()

    # ---------------------------------------------------------------- 写路径

    def append(
        self,
        event_type: str,
        payload: dict,
        idem_key: str,
        run_name: str,
        node_id: str | None = None,
    ) -> LedgerEntry:
        """追加一条事件并同步落盘（flush + fsync），返回已落账条目。

        idem_key 必须非空（幂等去重依赖它）；payload 必须可 JSON 序列化，且在落账前
        被深拷贝规范化——调用方之后修改原 dict 不影响已落账内容。
        本方法不做 idem 去重——重复投递允许落账，由 replay()/FlowState 只认第一条。
        """
        if not isinstance(idem_key, str) or not idem_key:
            raise ValueError("idem_key 必须是非空字符串（幂等去重依赖它）")
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("event_type 必须是非空字符串")
        if not isinstance(payload, dict):
            raise TypeError("payload 必须是 dict")
        try:
            # 深拷贝并规范化（可 JSON 序列化校验提前到落账前；调用方此后改原 dict 不影响账本）
            payload = json.loads(canonical_json(payload))
        except TypeError as serialize_error:
            raise TypeError(f"payload 必须可 JSON 序列化：{serialize_error}") from serialize_error
        if not isinstance(run_name, str) or not run_name:
            raise ValueError("run_name 必须是非空字符串")
        next_seq = self._entries_cache[-1].seq + 1 if self._entries_cache else 1
        entry = LedgerEntry(
            seq=next_seq,
            prev_hash=self._head_hash,
            idem=idem_key,
            type=event_type,
            run=run_name,
            node=node_id,
            ts=time.time(),
            payload=payload,
        )
        entry_hash = compute_entry_hash(entry)  # payload 不可序列化时在此抛 TypeError
        self._open_write_handle().write((canonical_json(entry.to_dict()) + "\n").encode("utf-8"))
        self._write_handle.flush()
        os.fsync(self._write_handle.fileno())
        self._entries_cache.append(entry)
        self._head_hash = entry_hash
        return entry

    # ---------------------------------------------------------------- 读路径

    def entries(self) -> list[LedgerEntry]:
        """全部完整条目（只读副本；崩溃留下的不完整尾行已不可见）。"""
        return list(self._entries_cache)

    def verify_chain(self) -> bool:
        """校验磁盘上 prev-link 哈希链 + seq 连续性；任何断裂/不可解析 → False。

        K5 增量校验：本实例内**已确认的前缀不再重复读盘 / 重算哈希**，只读上次
        校验点之后的新增字节并续算链——主循环每轮多次调用本方法，全量重读会让
        恢复退化成 O(N²) I/O。

        安全边界：只要文件比上次校验时更短（被截断 / 重写），就回退到从头全量
        校验；**新实例的首次调用永远是全量校验**，因此跨进程 / 跨实例的防篡改
        核对（含快照 head_hash 兜底）语义不变。同一实例内对"已确认前缀"的原地
        改写在增量路径下不可见——该实例自身只做追加，不覆盖历史。

        只读不修复；不完整尾行按恢复规则忽略。
        """
        try:
            (
                confirmed_entry_count,
                confirmed_head_hash,
                confirmed_byte_offset,
            ) = self._verify_chain_incrementally()
        except FileNotFoundError:
            return True  # 尚未落盘的空账本：链空即有效
        except (LedgerCorruptionError, OSError):
            return False
        self._confirmed_entry_count = confirmed_entry_count
        self._confirmed_head_hash = confirmed_head_hash
        self._confirmed_byte_offset = confirmed_byte_offset
        return True

    def _verify_chain_incrementally(self) -> tuple[int, str, int]:
        """从上次校验点续算链，返回 (已确认条目数, 链头哈希, 已确认字节偏移)。

        偏移只落在**完整行**（结尾换行符）之后，不完整的尾行留给下次校验；
        文件比上次校验时更短（被截断 / 重写）→ 从头全量校验。
        """
        if not self.ledger_path.exists():
            return 0, GENESIS_PREV_HASH, 0
        file_size = self.ledger_path.stat().st_size
        resume_offset = self._confirmed_byte_offset
        confirmed_entry_count = self._confirmed_entry_count
        expected_prev_hash = self._confirmed_head_hash
        if resume_offset == 0 or resume_offset > file_size:
            resume_offset = 0
            confirmed_entry_count = 0
            expected_prev_hash = GENESIS_PREV_HASH
        with open(self.ledger_path, "rb") as ledger_handle:
            ledger_handle.seek(resume_offset)
            tail_bytes = ledger_handle.read()
        line_chunks = tail_bytes.split(b"\n")
        trailing_chunk = line_chunks.pop()  # 结尾 b"" = 完整；否则是崩溃留下的半行
        confirmed_byte_offset = resume_offset + len(tail_bytes) - len(trailing_chunk)
        for line_index, line_bytes in enumerate(line_chunks, start=confirmed_entry_count + 1):
            if not line_bytes.strip():
                raise LedgerCorruptionError(f"{self.ledger_path} 第 {line_index} 行是空行")
            entry = self._parse_line(line_bytes, line_index)
            if entry.seq != confirmed_entry_count + 1 or entry.prev_hash != expected_prev_hash:
                raise LedgerCorruptionError(
                    f"{self.ledger_path} 第 {line_index} 行链断裂："
                    f"seq={entry.seq} / prev_hash 与既有前缀不接"
                )
            expected_prev_hash = compute_entry_hash(entry)
            confirmed_entry_count += 1
        return confirmed_entry_count, expected_prev_hash, confirmed_byte_offset

    def replay(self) -> FlowState:
        """全量重放：新 FlowState 投影全部条目（idem 幂等去重，只认第一条）。

        增量恢复（快照后续跑）不要用本方法——用 state.snapshots.recover_flow_state。
        """
        return FlowState().merge_from_events(self._entries_cache)

    # ---------------------------------------------------------------- 附加查询

    @property
    def last_seq(self) -> int:
        """最新条目的 seq；空账本为 0。（interfaces.md 之外的附加只读 API，见交付报告）"""
        return self._entries_cache[-1].seq if self._entries_cache else 0

    @property
    def head_hash(self) -> str:
        """最新条目的哈希；空账本为 GENESIS_PREV_HASH。（附加只读 API，见交付报告）"""
        return self._head_hash

    def close(self) -> None:
        """关闭追加用的文件句柄（幂等；只影响追加，读路径不依赖句柄）。"""
        if self._write_handle is not None:
            self._write_handle.close()
            self._write_handle = None

    def __enter__(self) -> EventLedger:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    # ---------------------------------------------------------------- 内部实现

    def _open_write_handle(self) -> IO[bytes]:
        if self._write_handle is None:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_handle = open(self.ledger_path, "ab")
        return self._write_handle

    def _truncate_incomplete_tail_line(self) -> None:
        """崩溃恢复：文件末尾没有换行符 → 最后一段是写了一半的行，截断丢弃。"""
        if not self.ledger_path.exists():
            return
        raw_bytes = self.ledger_path.read_bytes()
        if not raw_bytes or raw_bytes.endswith(b"\n"):
            return
        last_newline_offset = raw_bytes.rfind(b"\n")
        truncate_to_length = last_newline_offset + 1  # 一个换行符都没有则截为空文件
        with open(self.ledger_path, "r+b") as truncate_handle:
            truncate_handle.truncate(truncate_to_length)

    def _load_existing_entries(self) -> None:
        """构造时装载全部完整条目并重算链头；中段损坏对写者是致命的，直接抛错。"""
        try:
            disk_entries = self._read_complete_entries_from_disk()
        except FileNotFoundError:
            return
        self._entries_cache = disk_entries
        if disk_entries:
            self._head_hash = compute_entry_hash(disk_entries[-1])

    def _read_complete_entries_from_disk(self) -> list[LedgerEntry]:
        """解析磁盘全部完整行；无结尾换行符的最后一段（半行）按恢复规则丢弃。"""
        raw_bytes = self.ledger_path.read_bytes()
        if not raw_bytes:
            return []
        line_chunks = raw_bytes.split(b"\n")
        line_chunks.pop()  # 结尾 b"" = 文件以换行符收束；否则这就是崩溃留下的半行，丢弃
        parsed_entries = []
        for line_index, line_bytes in enumerate(line_chunks, start=1):
            if not line_bytes.strip():
                raise LedgerCorruptionError(f"{self.ledger_path} 第 {line_index} 行是空行")
            parsed_entries.append(self._parse_line(line_bytes, line_index))
        return parsed_entries

    def _parse_line(self, line_bytes: bytes, line_index: int) -> LedgerEntry:
        try:
            record = json.loads(line_bytes.decode("utf-8"))
            return LedgerEntry.from_dict(record)
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as parse_error:
            raise LedgerCorruptionError(
                f"{self.ledger_path} 第 {line_index} 行无法解析为 ledger 条目：{parse_error}"
            ) from parse_error
