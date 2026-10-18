"""域D2 · 内核闸测试（tasks.md T010）：budget 闸、hop 闸、retry 重试与耗尽、
超时分支。

崩溃恢复与人审拒绝路由见 test_kernel_engine_gates_recovery.py；
fan-in / 失败节点终态守卫见 test_kernel_engine_terminal_state_guards.py。

直跑：python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import kernel_engine_gates_support

import kernel_test_support as support  # noqa: E402
from kernel_engine_gates_support import (  # noqa: E402  (负责把 src/ 加进 sys.path)
    KernelGatesHarnessTestCase,
)


class KernelEngineGatesTestCase(KernelGatesHarnessTestCase):
    # ---------------------------------------------------------------- budget 闸

    def test_budget_gate_trips_before_flow_done(self) -> None:
        """两次投递各 100k token，预算 150k：最后一跳烧穿预算 → 转人审不宣告完成。"""
        first_task = support.make_node("T001")
        second_task = support.make_node("T002")
        flow_ir = support.make_flow_ir(
            [first_task, second_task],
            [support.make_edge("T001", "T002")],
            budget_tokens=150_000,
        )
        self.stubs.script_dispatch_result(
            "T001", support.make_dispatch_result(usage_tokens=100_000)
        )
        self.stubs.script_dispatch_result(
            "T002", support.make_dispatch_result(usage_tokens=100_000)
        )
        final_state = self.run_flow_once(flow_ir)
        self.assertFalse(final_state.is_done())
        self.assertTrue(final_state.is_paused())
        self.assertIn("budget", final_state.tripped_gates())
        self.assertEqual(final_state.token_spent(), 200_000)
        self.assertEqual(final_state.node_status("T002"), "done")
        gate_events = support.events_of_type(self.reopened_ledger(), "gate_tripped")
        self.assertEqual(gate_events[-1].payload["limit"], 150_000)

    # ---------------------------------------------------------------- hop 闸

    def test_hop_gate_trips_on_retry_storm(self) -> None:
        """retry:9 的节点持续失败：第 9 跳超过 4×2=8 的 hop 上限 → hop 闸先于 retry 耗尽。"""
        storm_task = support.make_node("T001", label="持续失败")
        downstream_task = support.make_node("T002")
        flow_ir = support.make_flow_ir(
            [storm_task, downstream_task],
            [support.make_edge("T001", "T002", retry=9)],
        )
        self.stubs.script_dispatch_result(
            "T001", *(support.make_dispatch_result(exit_code=3) for _ in range(12))
        )
        final_state = self.run_flow_once(flow_ir)
        self.assertTrue(final_state.is_paused())
        self.assertIn("hop", final_state.tripped_gates())
        self.assertNotIn("retry", final_state.tripped_gates())
        self.assertEqual(len(self.stubs.dispatch_calls), 9,
                         "hop 上限 4×2=8，第 9 次投递后下一轮闸停")
        ledger = self.reopened_ledger()
        gate_events = support.events_of_type(ledger, "gate_tripped")
        self.assertEqual(gate_events[-1].payload, {"gate": "hop", "actual": 9, "limit": 8})
        self.assertTrue(ledger.verify_chain())

    # ---------------------------------------------------------------- retry 边

    def test_retry_requeues_until_success(self) -> None:
        flaky_task = support.make_node("T001", label="闪失败")
        downstream_task = support.make_node("T002")
        flow_ir = support.make_flow_ir(
            [flaky_task, downstream_task],
            [support.make_edge("T001", "T002", retry=1)],
        )
        self.stubs.script_dispatch_result(
            "T001",
            support.make_dispatch_result(exit_code=3),
            support.make_dispatch_result(exit_code=0),
        )
        final_state = self.run_flow_once(flow_ir)
        self.assertTrue(final_state.is_done())
        self.assertEqual(final_state.node_status("T002"), "done")
        ledger = self.reopened_ledger()
        dispatch_idems = [
            entry.idem
            for entry in support.events_of_type(ledger, "agent_dispatched", "T001")
        ]
        self.assertEqual(
            dispatch_idems,
            ["release-login-T001-agent-dispatched-1", "release-login-T001-agent-dispatched-2"],
            "重试投递的 idem attempt 段必须递增",
        )
        self.assertEqual(len(support.events_of_type(ledger, "node_failed", "T001")), 1)
        requeue_events = support.events_of_type(ledger, "node_entered", "T001")
        self.assertEqual(requeue_events[0].payload["reason"], "retry")

    def test_retry_exhaustion_trips_retry_gate(self) -> None:
        flaky_task = support.make_node("T001")
        downstream_task = support.make_node("T002")
        flow_ir = support.make_flow_ir(
            [flaky_task, downstream_task],
            [support.make_edge("T001", "T002", retry=1)],
        )
        self.stubs.script_dispatch_result(
            "T001",
            support.make_dispatch_result(exit_code=3),
            support.make_dispatch_result(exit_code=3),
        )
        final_state = self.run_flow_once(flow_ir)
        self.assertTrue(final_state.is_paused())
        self.assertIn("retry", final_state.tripped_gates())
        self.assertEqual(len(self.stubs.dispatch_calls), 2, "retry:1 = 共两次尝试")
        gate_events = support.events_of_type(self.reopened_ledger(), "gate_tripped")
        self.assertEqual(gate_events[-1].payload["actual"], 2)
        self.assertEqual(gate_events[-1].payload["limit"], 1)

    # ---------------------------------------------------------------- 超时分支

    def test_timeout_branch_edge_taken_on_dispatch_timeout(self) -> None:
        """定时器先到即走超时分支边（design.md §4.1）：源节点不落 retry 闸。"""
        slow_task = support.make_node("T001", label="会超时")
        fallback_task = support.make_node("T002", label="超时诊断")
        flow_ir = support.make_flow_ir(
            [slow_task, fallback_task],
            [support.make_edge("T001", "T002", timeout_seconds=60.0)],
        )
        self.stubs.script_dispatch_result(
            "T001", support.make_dispatch_result(exit_code=None, timed_out=True)
        )
        final_state = self.run_flow_once(flow_ir)
        self.assertTrue(final_state.is_done())
        self.assertEqual(self.stubs.dispatch_calls[0][1], 60.0,
                         "出边 timeout_seconds 应作为该节点的投递超时传入")
        ledger = self.reopened_ledger()
        exit_events = support.events_of_type(ledger, "agent_exited", "T001")
        self.assertTrue(exit_events[0].payload["timed_out"])
        taken_targets = [
            entry.payload["target"]
            for entry in support.events_of_type(ledger, "edge_taken")
        ]
        self.assertEqual(taken_targets, ["T002"])
        self.assertEqual(final_state.node_status("T002"), "done")
        self.assertEqual(final_state.node_status("T001"), "done",
                         "走超时分支后源节点经 edge_taken 投影为 done")


if __name__ == "__main__":
    unittest.main()
