"""域D2 · 内核终态守卫测试（tasks.md T010）：已终态节点不得被晚到的边拖回活跃。

覆盖 FIX-B1（已 done 的 join 不被 skipped 源的分支边复活）与 K3（重试耗尽的 failed
节点不被晚到入边复活，resume 后也不得绕过重试策略与 retry 闸），另含两条
"合法重投 / 超时分支仍生效"的边界，以及节点成功判定的双信号组合（T020o 缺口②：
``exit≠0`` + ``semantic_done=True`` + 产物齐 → ``node_done``，无 ``node_failed``）。

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
from flowrun.ir.schema import NODE_KIND_JOIN  # noqa: E402
from flowrun.state.ledger import EventLedger  # noqa: E402


class KernelEngineTerminalStateGuardsTestCase(KernelGatesHarnessTestCase):
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
        seeded_ledger = EventLedger(self.run_dir() / "ledger.jsonl")
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
        self.assertIn("retry_exhausted", first_state.tripped_gates())
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
        seeded_ledger = EventLedger(self.run_dir() / "ledger.jsonl")
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
        seeded_ledger = EventLedger(self.run_dir() / "ledger.jsonl")
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

    # ---------------------------------------------------------------- 节点成功判定（双信号组合）

    def test_nonzero_exit_with_semantic_done_and_outputs_completes_node(self) -> None:
        """T020o 缺口②：``exit≠0`` + ``semantic_done=True`` + 产物齐 → ``node_done``。

        成功判定是 ``not timed_out and (exit_code == 0 or semantic_done)``
        （engine_node_executors）：headless CLI 常见的"跑完已报完成、但退出码非零"
        正是靠那个 ``or`` 成立的——agent 已把产物写好并给出语义完成信号，就不该因
        非零退出码把节点判失败。修复前判定节点没有 agent_exited，这条组合在节点级
        一直无测；本用例把它钉住：node_done 恰一条、node_failed 零条、不落 retry 闸。
        """
        task_node = support.make_node(
            "T001", label="非零退出但已报完成", outputs=["report/out.txt"]
        )
        flow_ir = support.make_flow_ir([task_node], [])
        (self.run_dir() / "report").mkdir(parents=True, exist_ok=True)
        (self.run_dir() / "report/out.txt").write_text("done", encoding="utf-8")
        self.stubs.script_dispatch_result(
            "T001", support.make_dispatch_result(exit_code=1, semantic_done=True)
        )

        final_state = self.run_flow_once(flow_ir)

        self.assertTrue(final_state.is_done())
        self.assertEqual(final_state.node_status("T001"), "done")
        ledger = self.reopened_ledger()
        self.assertEqual(
            support.events_of_type(ledger, "node_failed", "T001"), [],
            "语义完成信号 + 产物齐时，非零退出码不得把节点判失败",
        )
        self.assertEqual(len(support.events_of_type(ledger, "node_done", "T001")), 1)
        self.assertNotIn("retry_exhausted", final_state.tripped_gates())
        exit_events = support.events_of_type(ledger, "agent_exited", "T001")
        self.assertEqual(exit_events[0].payload["exit_code"], 1)
        self.assertTrue(exit_events[0].payload["semantic_done"])

    def test_nonzero_exit_with_semantic_done_but_missing_output_still_fails(self) -> None:
        """对照：``semantic_done`` 只替代退出码，不替代产物契约——产物缺失仍 node_failed。"""
        task_node = support.make_node(
            "T001", label="非零退出但已报完成", outputs=["report/out.txt"]
        )
        flow_ir = support.make_flow_ir([task_node], [])
        self.stubs.script_dispatch_result(
            "T001", support.make_dispatch_result(exit_code=1, semantic_done=True)
        )

        final_state = self.run_flow_once(flow_ir)

        self.assertEqual(final_state.node_status("T001"), "failed")
        failed_events = support.events_of_type(
            self.reopened_ledger(), "node_failed", "T001"
        )
        self.assertEqual(len(failed_events), 1)
        self.assertEqual(failed_events[0].payload["reason"], "missing_outputs")
        self.assertEqual(failed_events[0].payload["missing"], ["report/out.txt"])


if __name__ == "__main__":
    unittest.main()
