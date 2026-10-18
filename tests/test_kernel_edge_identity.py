"""域D2 · 边幂等键唯一标识一条边（缺陷 2）。

旧键 ``<run>-edge-<source>-<target>`` 不含"哪一条边"的信息：A→B 有多条边时
（例如一条无条件边 + 一条 barrier 边，二者都会在 A done 时激活）它们撞成同一个
幂等键，第二条被当成"重复投递"丢弃 → 账本少记一条边激活，审计失真。

新键格式：``<run>-edge-<index>-<source>-><target>-<kind>``（index = 该边在
``flow_ir.edges`` 里的声明序号，kind ∈ when/retry/timeout/auto）。
注意：这是**账本事件键的格式变更**，旧账本跨版本续跑时边幂等键不兼容。

直跑：python -m unittest discover -s tests
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))

import kernel_test_support as support  # noqa: E402
from flowrun.kernel.engine import run_flow  # noqa: E402
from flowrun.kernel.engine_gates import edge_idem_key, edge_kind  # noqa: E402
from flowrun.state.ledger import EventLedger  # noqa: E402


class EdgeIdentityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_edge_identity_")
        self.addCleanup(self._temp_dir.cleanup)
        self.workspace = Path(self._temp_dir.name) / "workspace"
        self.stubs = support.KernelHookStubs()

    def run_flow_once(self, flow_ir) -> object:
        return asyncio.run(run_flow(flow_ir, self.workspace, self.stubs.build_hooks()))

    def reopened_ledger(self) -> EventLedger:
        ledger = EventLedger(self.workspace / "ledger.jsonl")
        self.addCleanup(ledger.close)
        return ledger

    def edge_taken_to_target(self, source_id: str, target_id: str) -> list:
        return [
            entry
            for entry in support.events_of_type(self.reopened_ledger(), "edge_taken", source_id)
            if entry.payload.get("target") == target_id
        ]

    # ---------------------------------------------------------------- 同源同目标多条边

    def test_two_activatable_edges_same_pair_are_both_recorded(self) -> None:
        """缺陷 2 复现：A→B 有两条都会激活的边 → **两条都落账**（旧实现只留一条）。"""
        flow_ir = support.make_flow_ir(
            [support.make_node("A001"), support.make_node("B001")],
            [
                support.make_edge("A001", "B001"),               # 无条件边（auto）
                support.make_edge("A001", "B001", when="all"),   # barrier 边（when）
            ],
        )

        final_state = self.run_flow_once(flow_ir)

        self.assertEqual(final_state.node_status("B001"), "done")
        taken_edges = self.edge_taken_to_target("A001", "B001")
        self.assertEqual(len(taken_edges), 2, "同源同目标的两条边必须各落一条 edge_taken")
        self.assertEqual(
            [entry.payload["kind"] for entry in taken_edges], ["auto", "when"]
        )
        self.assertEqual(
            len({entry.idem for entry in taken_edges}), 2, "两条边的幂等键必须各不相同"
        )
        for entry in taken_edges:
            self.assertIn(f"{flow_ir.run_name}-edge-", entry.idem)

    def test_barrier_and_timeout_edges_to_same_target_have_distinct_keys(self) -> None:
        """缺陷 2：when:all + timeout 这类同源同目标的不同类别边，键必须互不相同。"""
        barrier_edge = support.make_edge("A001", "B001", when="all")
        timeout_edge = support.make_edge("A001", "B001", timeout_seconds=1800.0)
        retry_edge = support.make_edge("A001", "B001", retry=2)
        unconditional_edge = support.make_edge("A001", "B001")
        flow_ir = support.make_flow_ir(
            [support.make_node("A001"), support.make_node("B001")],
            [barrier_edge, timeout_edge, retry_edge, unconditional_edge],
        )

        idem_keys = [
            edge_idem_key(flow_ir, edge)
            for edge in (barrier_edge, timeout_edge, retry_edge, unconditional_edge)
        ]

        self.assertEqual(len(set(idem_keys)), 4, "四条同类目标的边必须四个不同幂等键")
        self.assertEqual(
            [edge_kind(edge) for edge in
             (barrier_edge, timeout_edge, retry_edge, unconditional_edge)],
            ["when", "timeout", "retry", "auto"],
        )
        self.assertEqual(
            idem_keys[0],
            f"{flow_ir.run_name}-edge-0-A001->B001-when",
            "幂等键须可读：run-edge-<index>-<source>-><target>-<kind>",
        )

    def test_duplicate_identical_edges_get_distinct_keys(self) -> None:
        """端到端完全重复的两条边（同 source/target/when）：靠序号仍各得其所。"""
        first_edge = support.make_edge("A001", "B001")
        second_edge = support.make_edge("A001", "B001")
        flow_ir = support.make_flow_ir(
            [support.make_node("A001"), support.make_node("B001")],
            [first_edge, second_edge],
        )

        self.assertNotEqual(
            edge_idem_key(flow_ir, first_edge), edge_idem_key(flow_ir, second_edge)
        )

    # ---------------------------------------------------------------- 单边幂等去重仍有效

    def test_single_edge_dedup_still_effective(self) -> None:
        """普通单边图：重复投递同一条边只记一次（幂等去重不得被本次修复破坏）。"""
        flow_ir = support.make_flow_ir(
            [support.make_node("A001"), support.make_node("B001")],
            [support.make_edge("A001", "B001")],
        )

        self.assertTrue(self.run_flow_once(flow_ir).is_done())
        events_after_first_run = len(self.reopened_ledger().entries())
        self.assertEqual(len(self.edge_taken_to_target("A001", "B001")), 1)

        second_state = self.run_flow_once(flow_ir)

        self.assertTrue(second_state.is_done())
        self.assertEqual(
            len(self.reopened_ledger().entries()), events_after_first_run,
            "续跑不得重复投递同一条边",
        )
        self.assertEqual(len(self.edge_taken_to_target("A001", "B001")), 1)


if __name__ == "__main__":
    unittest.main()
