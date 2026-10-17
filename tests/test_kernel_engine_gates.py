"""域D2 · 内核闸与恢复测试（tasks.md T010）：budget 闸、hop 闸、retry 重试与
耗尽、超时分支、崩溃恢复、human deny 路由、ledger 链校验 + 重放一致。

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
from flowrun.ir.schema import NODE_KIND_HUMAN, NODE_KIND_JOIN  # noqa: E402

from flowrun.kernel.engine import run_flow  # noqa: E402
from flowrun.state.ledger import EventLedger  # noqa: E402


class KernelEngineGatesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_gates_test_")
        self.workspace = Path(self._temp_dir.name) / "workspace"
        self.stubs = support.KernelHookStubs()

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def run_flow_once(self, flow_ir) -> object:
        return asyncio.run(run_flow(flow_ir, self.workspace, self.stubs.build_hooks()))

    def reopened_ledger(self) -> EventLedger:
        return EventLedger(self.workspace / "ledger.jsonl")

    # ---------------------------------------------------------------- fan-in 终态守卫（FIX-B1）

    def test_skipped_predecessor_does_not_reactivate_done_join(self) -> None:
        """FIX-B1 join 变体：已 done 的 join 不得被晚到的 barrier 边（源为 skipped）
        拖回 ready 重复 node_done。账本手工播种到"join 前驱 A done、M skipped"状态。"""
        entry_task = support.make_node("A001")
        skipped_branch = support.make_node("M001")
        join_node = support.make_node("X001", kind=NODE_KIND_JOIN, label="等齐")
        after_join = support.make_node("Y001")
        flow_ir = support.make_flow_ir(
            [entry_task, skipped_branch, join_node, after_join],
            [
                support.make_edge("A001", "X001"),
                support.make_edge("A001", "M001"),
                support.make_edge("M001", "X001", when="all"),
                support.make_edge("X001", "Y001"),
            ],
        )
        seeded_ledger = EventLedger(self.workspace / "ledger.jsonl")
        seeded_ledger.append(
            "flow_started",
            {"entry_nodes": ["A001"], "planned_nodes": ["A001", "M001", "X001", "Y001"]},
            "release-login-flow-started",
            flow_ir.run_name,
        )
        seeded_ledger.append(
            "node_done", {"artifacts": []}, "release-login-A001-node-done-1",
            flow_ir.run_name, "A001",
        )
        seeded_ledger.append(
            "edge_taken", {"source": "A001", "target": "X001"},
            "release-login-edge-A001-X001", flow_ir.run_name, "A001",
        )
        seeded_ledger.append(
            "edge_taken", {"source": "A001", "target": "M001"},
            "release-login-edge-A001-M001", flow_ir.run_name, "A001",
        )
        seeded_ledger.append(
            "user_directive", {"directive": "skip"}, "release-login-M001-user-directive-skip-1",
            flow_ir.run_name, "M001",
        )
        seeded_ledger.close()

        final_state = self.run_flow_once(flow_ir)
        self.assertTrue(final_state.is_done())
        ledger = self.reopened_ledger()
        self.assertEqual(
            len(support.events_of_type(ledger, "node_done", "X001")), 1,
            "已 done 的 join 不得被 barrier 边拖回 ready 重跑",
        )
        self.assertEqual(
            support.events_of_type(ledger, "edge_taken", "M001"), [],
            "target 已终态时该边不激活（edge_taken 不落——边未被走）",
        )
        self.assertEqual(final_state.node_status("M001"), "skipped")
        self.assertEqual([call[0] for call in self.stubs.dispatch_calls], ["Y001"])

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

    # ---------------------------------------------------------------- 崩溃恢复

    def test_crash_interrupted_running_node_fails_then_recovers(self) -> None:
        """账本停在 agent_dispatched（无 agent_exited）：视为中断落 node_failed，
        retry 边重新入队后 attempt 递增重投直至完成。"""
        flaky_task = support.make_node("T001")
        downstream_task = support.make_node("T002")
        flow_ir = support.make_flow_ir(
            [flaky_task, downstream_task],
            [support.make_edge("T001", "T002", retry=1)],
        )
        crashed_ledger = EventLedger(self.workspace / "ledger.jsonl")
        crashed_ledger.append(
            "flow_started",
            {"entry_nodes": ["T001"], "planned_nodes": ["T001", "T002"]},
            "release-login-flow-started",
            flow_ir.run_name,
        )
        crashed_ledger.append(
            "agent_dispatched",
            {"attempt": 1},
            "release-login-T001-agent-dispatched-1",
            flow_ir.run_name,
            "T001",
        )
        crashed_ledger.close()

        final_state = self.run_flow_once(flow_ir)
        self.assertTrue(final_state.is_done())
        ledger = self.reopened_ledger()
        interrupted_failures = support.events_of_type(ledger, "node_failed", "T001")
        self.assertEqual(len(interrupted_failures), 1)
        self.assertIsNone(interrupted_failures[0].payload["exit_code"])
        self.assertEqual(interrupted_failures[0].payload["reason"], "interrupted_by_restart")
        self.assertEqual(
            [call[0] for call in self.stubs.dispatch_calls], ["T001", "T002"],
            "崩溃前的半次投递不得重复（T001 只以 attempt=2 重投一次，随后正常推进 T002）",
        )
        engine_dispatch_idems = [
            entry.idem
            for entry in support.events_of_type(ledger, "agent_dispatched", "T001")
        ]
        self.assertEqual(len(engine_dispatch_idems), 2)
        self.assertIn("release-login-T001-agent-dispatched-2", engine_dispatch_idems)
        self.assertTrue(ledger.verify_chain())
        self.assertEqual(ledger.replay().to_dict(), final_state.to_dict(),
                         "增量恢复必须与全量重放语义一致")

    # ---------------------------------------------------------------- human deny

    def test_human_deny_routes_node_to_retry_gate(self) -> None:
        task_node = support.make_node("T001")
        human_node = support.make_node("H001", kind=NODE_KIND_HUMAN, label="发布人审")
        release_task = support.make_node("T002")
        flow_ir = support.make_flow_ir(
            [task_node, human_node, release_task],
            [support.make_edge("T001", "H001"), support.make_edge("H001", "T002")],
        )
        first_state = self.run_flow_once(flow_ir)
        self.assertEqual(first_state.node_status("H001"), "awaiting_human")

        deny_ledger = self.reopened_ledger()
        deny_ledger.append(
            "human_resolved",
            {"decision": "deny"},
            "release-login-H001-human-resolved-1",
            flow_ir.run_name,
            "H001",
        )
        deny_ledger.close()

        final_state = self.run_flow_once(flow_ir)
        self.assertEqual(final_state.node_status("H001"), "failed")
        self.assertIn("retry", final_state.tripped_gates())
        self.assertTrue(final_state.is_paused())
        self.assertEqual(final_state.node_status("T002"), "planned")


    # ---------------------------------------------------------------- 失败节点终态守卫（K3）

    def test_failed_node_is_not_revived_by_late_incoming_edge(self) -> None:
        """K3 复现：X001 重试耗尽失败后，另一分支 M001 的晚到入边不得把它退回 ready。

        修复前 _apply_edge_taken / ⑤ 层只守 done/skipped，failed 被晚到边复活；
        resume 后它以 attempt=2 重跑，绕过重试策略与 retry 闸。"""
        entry_task = support.make_node("A001")
        late_branch_task = support.make_node("M001", label="晚到分支")
        failing_task = support.make_node("X001", label="重试耗尽")
        flow_ir = support.make_flow_ir(
            [entry_task, late_branch_task, failing_task],
            [
                support.make_edge("A001", "X001"),
                support.make_edge("A001", "M001"),
                support.make_edge("M001", "X001"),
            ],
        )
        self.stubs.script_dispatch_result(
            "X001", *(support.make_dispatch_result(exit_code=3) for _ in range(5))
        )

        first_state = self.run_flow_once(flow_ir)

        self.assertEqual(first_state.node_status("X001"), "failed")
        self.assertIn("retry", first_state.tripped_gates())
        first_ledger = self.reopened_ledger()
        late_edges = [
            edge_entry
            for edge_entry in support.events_of_type(first_ledger, "edge_taken")
            if edge_entry.payload.get("source") == "M001"
            and edge_entry.payload.get("target") == "X001"
        ]
        self.assertEqual(
            late_edges, [], "failed 的 target 不得被晚到入边激活（账本不留假事实）"
        )

        resume_ledger = self.reopened_ledger()
        resume_ledger.append(
            "flow_resumed", {}, f"{flow_ir.run_name}-flow-resumed-1", flow_ir.run_name
        )
        resume_ledger.close()

        final_state = self.run_flow_once(flow_ir)

        self.assertEqual(final_state.node_status("X001"), "failed")
        self.assertEqual(
            len(support.events_of_type(self.reopened_ledger(), "agent_dispatched", "X001")),
            1,
            "失败节点不得被晚到边复活后以 attempt=2 重投",
        )

    def test_ledger_seeded_failed_node_with_retry_edge_is_still_requeued(self) -> None:
        """K3 边界：失败节点的**合法**重投路径不受影响——重试边的 node_entered 仍生效。"""
        flaky_task = support.make_node("T001", label="闪失败")
        downstream_task = support.make_node("T002")
        flow_ir = support.make_flow_ir(
            [flaky_task, downstream_task],
            [support.make_edge("T001", "T002", retry=2)],
        )
        seeded_ledger = EventLedger(self.workspace / "ledger.jsonl")
        seeded_ledger.append(
            "flow_started",
            {"entry_nodes": ["T001"], "planned_nodes": ["T001", "T002"]},
            f"{flow_ir.run_name}-flow-started",
            flow_ir.run_name,
        )
        seeded_ledger.append(
            "node_failed", {"exit_code": 3},
            f"{flow_ir.run_name}-T001-node-failed-1", flow_ir.run_name, "T001",
        )
        seeded_ledger.close()
        self.stubs.script_dispatch_result("T001", support.make_dispatch_result(exit_code=0))

        final_state = self.run_flow_once(flow_ir)

        self.assertTrue(final_state.is_done())
        self.assertEqual(final_state.node_status("T001"), "done")
        requeue_events = support.events_of_type(self.reopened_ledger(), "node_entered", "T001")
        self.assertEqual(
            requeue_events[0].payload["reason"], "retry",
            "failed → ready 必须仍能由失败处置策略的 node_entered 承载",
        )

    def test_ledger_seeded_timed_out_node_still_walks_timeout_branch(self) -> None:
        """K3 边界：超时分支仍是失败节点的合法激活路径（failed 源 → 分支 target ready）。"""
        slow_task = support.make_node("T001", label="会超时")
        fallback_task = support.make_node("T002", label="超时诊断")
        flow_ir = support.make_flow_ir(
            [slow_task, fallback_task],
            [support.make_edge("T001", "T002", timeout_seconds=60.0)],
        )
        seeded_ledger = EventLedger(self.workspace / "ledger.jsonl")
        seeded_ledger.append(
            "flow_started",
            {"entry_nodes": ["T001"], "planned_nodes": ["T001", "T002"]},
            f"{flow_ir.run_name}-flow-started",
            flow_ir.run_name,
        )
        seeded_ledger.append(
            "agent_exited", {"exit_code": None, "timed_out": True},
            f"{flow_ir.run_name}-T001-agent-exited-1", flow_ir.run_name, "T001",
        )
        seeded_ledger.append(
            "node_failed", {"exit_code": None, "timed_out": True},
            f"{flow_ir.run_name}-T001-node-failed-1", flow_ir.run_name, "T001",
        )
        seeded_ledger.close()

        final_state = self.run_flow_once(flow_ir)

        self.assertTrue(final_state.is_done())
        self.assertEqual(final_state.node_status("T002"), "done")
        taken_targets = [
            edge_entry.payload["target"]
            for edge_entry in support.events_of_type(self.reopened_ledger(), "edge_taken")
        ]
        self.assertEqual(taken_targets, ["T002"])


if __name__ == "__main__":
    unittest.main()
