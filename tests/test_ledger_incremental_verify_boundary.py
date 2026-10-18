"""K5 边界钉子：增量校验对"已确认前缀的原地改写"不可见（**有意保留的边界**）。

直跑：python -m unittest discover -s tests

背景（决策记录，不是缺陷）：``verify_chain()`` 在本实例内增量推进——已确认前缀
只读一次、不重算哈希，使主循环每轮的链校验保持 O(新增条目) 而非 O(N)。代价是：
同一实例内把**已确认前缀**里的某一行原地改成"等长且自洽"的内容，增量路径看不见。

本轮决策：**钉住边界**（而非"把前缀也纳入每轮校验"的 narrow 方案）。
理由有两条，分别由本文件的两个测试固定：

1. ``test_fresh_instance_detects_the_same_rewrite`` —— 兜底通道确实存在：
   关闭后重新打开（新实例首次调用必全量）一定能检出该改写；
2. ``test_no_op_incremental_verify_rehashes_nothing`` —— 若把前缀纳入每次校验，
   无新增时也会重算全部哈希（O(N)/轮 → 整体 O(N²) 读盘），与 K5 目标冲突。
   该测试一旦变红，就说明有人把 narrow 方案塞回来了。
"""

import sys
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import ledger_test_support

from ledger_test_support import (  # noqa: E402  (负责把 src/ 加进 sys.path)
    SAMPLE_EVENTS,
    LedgerTestCase,
    append_sample_events,
)
from flowrun.state import ledger as ledger_module  # noqa: E402
from flowrun.state.ledger import EventLedger, canonical_json  # noqa: E402


def count_entry_hash_calls(invoke) -> int:
    """统计 invoke() 期间 compute_entry_hash 的调用次数（增量性的直接观测量）。"""
    call_counter = {"count": 0}
    original_compute_entry_hash = ledger_module.compute_entry_hash

    def counting_compute_entry_hash(entry):
        call_counter["count"] += 1
        return original_compute_entry_hash(entry)

    ledger_module.compute_entry_hash = counting_compute_entry_hash
    try:
        invoke()
    finally:
        ledger_module.compute_entry_hash = original_compute_entry_hash
    return call_counter["count"]


class IncrementalVerifyPrefixBoundaryTest(LedgerTestCase):
    """同一实例的增量校验只对"新增字节"负责；已确认前缀的等长原地改写不可见。"""

    def rewrite_line_keeping_byte_length(
        self, line_index: int, mutated_record: dict
    ) -> None:
        """等长替换磁盘上的一行：文件总长不变，故增量路径不会回退到全量校验。"""
        raw_lines = self.ledger_path.read_bytes().split(b"\n")
        original_line = raw_lines[line_index]
        replacement_line = canonical_json(mutated_record).encode("utf-8")
        self.assertEqual(
            len(replacement_line),
            len(original_line),
            "夹具前提：替换行必须与原文等长，否则触发的是位移/截断而非'原地改写'",
        )
        raw_lines[line_index] = replacement_line
        self.ledger_path.write_bytes(b"\n".join(raw_lines))

    def tamper_confirmed_agent_exited_entry(self, ledger: EventLedger) -> None:
        """把已确认前缀内 agent_exited 的 payload.tokens 1200 → 9999（等长改写）。

        必须选**非末条**、且已落入已确认前缀的条目：末条改动要等"下一条落账"才可能
        被发现；而非末尾条目在**全量**校验里会经由下一行的 prev_hash 暴露。
        """
        target_index = next(
            index
            for index, entry in enumerate(ledger.entries())
            if entry.type == "agent_exited"
        )
        raw_lines = self.ledger_path.read_bytes().split(b"\n")
        tampered_record = ledger.entries()[target_index].to_dict()
        self.assertEqual(tampered_record["payload"]["tokens"], 1200)
        tampered_record["payload"]["tokens"] = 9999
        replacement_line = canonical_json(tampered_record).encode("utf-8")
        self.assertEqual(
            len(replacement_line), len(raw_lines[target_index]), "等长改写前提"
        )
        raw_lines[target_index] = replacement_line
        self.ledger_path.write_bytes(b"\n".join(raw_lines))

    def test_confirmed_prefix_rewrite_is_invisible_to_the_same_instance(self) -> None:
        """已知边界：同一实例的增量校验看不到已确认前缀的原地改写（钉住该边界）。"""
        ledger = self.make_ledger()
        append_sample_events(ledger, len(SAMPLE_EVENTS))
        self.assertTrue(ledger.verify_chain())  # 前缀至此全部确认
        self.tamper_confirmed_agent_exited_entry(ledger)
        self.assertTrue(
            ledger.verify_chain(),
            "边界：增量路径只看新增字节，已确认前缀的等长原地改写对它不可见",
        )

    def test_fresh_instance_detects_the_same_rewrite(self) -> None:
        """兜底通道：跨实例（关闭后重开）必然全量校验，必须检出同一处改写。"""
        ledger = self.make_ledger()
        append_sample_events(ledger, len(SAMPLE_EVENTS))
        self.assertTrue(ledger.verify_chain())
        self.tamper_confirmed_agent_exited_entry(ledger)
        ledger.close()

        fresh_ledger_view = EventLedger.open_readonly(self.ledger_path)
        self.addCleanup(fresh_ledger_view.close)
        self.assertFalse(
            fresh_ledger_view.verify_chain(),
            "新实例首次调用是全量校验——这是本边界的兜底，必须检出",
        )

    def test_no_op_incremental_verify_rehashes_nothing(self) -> None:
        """为何选 pin 而非 narrow：无新增字节时，增量校验重算 0 条哈希。"""
        ledger = self.make_ledger()
        append_sample_events(ledger, len(SAMPLE_EVENTS))
        self.assertTrue(ledger.verify_chain())
        hash_call_count = count_entry_hash_calls(lambda: ledger.verify_chain())
        self.assertEqual(
            hash_call_count,
            0,
            "把已确认前缀纳入每轮校验即回到 O(N)/轮（整体 O(N²)）——本测试防其回归",
        )


if __name__ == "__main__":
    unittest.main()
