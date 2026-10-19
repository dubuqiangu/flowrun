"""域D2 · 内核恢复与失败路由测试（tasks.md T010）：账本停在 agent_dispatched 的崩溃恢复
（视为中断落 node_failed，retry 边重新入队后 attempt 递增重投直至完成）、
human deny 把节点路由到 retry 闸，以及判定节点的崩溃恢复（T020a 窄边界）。

直跑：python -m unittest discover -s tests
"""

import asyncio
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
from flowrun.ir.schema import NODE_KIND_DECISION, NODE_KIND_HUMAN  # noqa: E402
from flowrun.kernel.engine import EngineHooks, run_flow  # noqa: E402
from flowrun.kernel.judge import (  # noqa: E402
    JudgeDispatchFacts,
    JudgeVerdict,
)
from flowrun.state.ledger import EventLedger  # noqa: E402


class KernelEngineRecoveryTestCase(KernelGatesHarnessTestCase):
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


class DecisionNodeTimeoutRecoveryTestCase(KernelGatesHarnessTestCase):
    """T020a 窄边界：判定节点的 ``agent_exited`` 与崩溃恢复的失败路由。

    T020a 让判定路径也会落 ``agent_exited``（此前判定节点没有任何退出事件）。由此
    出现一条修复前不可能成立的组合：判定投递超时（``timed_out=True``）、在 ``verdict``
    落账前进程崩溃 —— 恢复时 ``resolve_failed_node_policy`` 读到的"最近一次
    agent_exited"带着超时事实，于是走**超时分支**而不是重试重新入队 / retry 闸。

    本类钉三件事：① 该组合的现状（超时分支）；② 对照（账本里没有该退出事件时落 retry
    闸——T020a 之前的形状）；③ 前置条件的回归位（判定投递超时的事实确实落到判定节点
    的 agent_exited，T020a 被回退即变红）。
    """

    def make_timeout_branch_flow_ir(self):
        """判定节点带两种出边：谓词边与超时分支边（便于区分走了哪条）。

        刻意**不**给重试边：这样"是否重新入队"与"是否落 retry 闸"才是两条路径各自的
        独有信号（给了重试边时，它同时是一条 forward 自动边，超时分支把源节点投影为
        done 后它还会正常激活，反而盖掉差异——见 engine_gates.edge_kind /
        engine_graph.outgoing_activatable_edges 的边分类）。
        """
        decision_node = support.make_node("D001", kind=NODE_KIND_DECISION, label="验收?")
        verdict_target = support.make_node("T002", label="判定通过分支")
        timeout_target = support.make_node("T003", label="超时诊断")
        flow_ir = support.make_flow_ir(
            [decision_node, verdict_target, timeout_target],
            [
                support.make_edge("D001", "T002", when="ok"),
                support.make_edge("D001", "T003", timeout_seconds=5.0),
            ],
        )
        return flow_ir, verdict_target, timeout_target

    def seed_crashed_decision_dispatch(self, flow_ir, with_timeout_exit: bool) -> None:
        """播种"判定已投递、随后崩溃"的账本（形状与生产判定路径落账一致）。"""
        seeded_ledger = EventLedger(self.workspace / "ledger.jsonl")
        seeded_ledger.append(
            "flow_started",
            {"entry_nodes": ["D001"], "planned_nodes": ["D001", "T002", "T003"]},
            f"{flow_ir.run_name}-flow-started",
            flow_ir.run_name,
        )
        seeded_ledger.append(
            "agent_dispatched",
            {"attempt": 1, "purpose": "judge"},
            f"{flow_ir.run_name}-D001-agent-dispatched-1",
            flow_ir.run_name,
            "D001",
        )
        if with_timeout_exit:
            seeded_ledger.append(
                "agent_exited",
                {"exit_code": None, "timed_out": True, "semantic_done": True},
                f"{flow_ir.run_name}-D001-agent-exited-1",
                flow_ir.run_name,
                "D001",
            )
        seeded_ledger.close()

    def test_crashed_timed_out_decision_dispatch_walks_timeout_branch(self) -> None:
        """窄边界现状：判定超时事实 + 崩溃 → 超时分支边（不重新入队、不落 retry 闸）。"""
        flow_ir, verdict_target, timeout_target = self.make_timeout_branch_flow_ir()
        self.seed_crashed_decision_dispatch(flow_ir, with_timeout_exit=True)

        final_state = self.run_flow_once(flow_ir)

        ledger = self.reopened_ledger()
        taken_targets = [
            entry.payload["target"] for entry in support.events_of_type(ledger, "edge_taken")
        ]
        self.assertEqual(
            taken_targets,
            [timeout_target.node_id],
            "最近一次 agent_exited 带 timed_out → 走超时分支边",
        )
        self.assertNotIn(verdict_target.node_id, taken_targets)
        self.assertNotIn("retry", final_state.tripped_gates())
        self.assertEqual(
            support.events_of_type(ledger, "node_entered", "D001"),
            [],
            "已走超时分支就不再重新入队（两条路径互斥）",
        )
        self.assertEqual(
            len(support.events_of_type(ledger, "agent_dispatched", "D001")),
            1,
            "走超时分支不重新判定（判定只投递过那一次）",
        )
        failed_events = support.events_of_type(ledger, "node_failed", "D001")
        self.assertEqual(
            [entry.payload["reason"] for entry in failed_events],
            ["interrupted_by_restart"],
            "崩溃恢复落的中断失败事实不变，只有其处置分支受超时事实影响",
        )
        self.assertTrue(final_state.is_done())

    def test_crashed_decision_dispatch_without_exit_event_trips_retry_gate(self) -> None:
        """对照（T020a 之前的账本形状）：没有 agent_exited → 不判超时 → 落 retry 闸。"""
        flow_ir, _verdict_target, timeout_target = self.make_timeout_branch_flow_ir()
        self.seed_crashed_decision_dispatch(flow_ir, with_timeout_exit=False)

        final_state = self.run_flow_once(flow_ir)

        self.assertIn(
            "retry", final_state.tripped_gates(), "没有超时事实 → 退回重试耗尽闸"
        )
        self.assertTrue(final_state.is_paused())
        taken_targets = [
            entry.payload["target"]
            for entry in support.events_of_type(self.reopened_ledger(), "edge_taken")
        ]
        self.assertNotIn(
            timeout_target.node_id, taken_targets, "没有超时事实就不得走超时分支"
        )

    def test_timed_out_judge_dispatch_lands_agent_exited_on_decision_node(self) -> None:
        """回归位（T020a）：判定投递超时的事实必须落到判定节点的 agent_exited。

        这是上方窄边界的前置条件——若判定节点的 ``agent_exited`` 入账被回退，
        本用例立刻变红（账本里没有任何带 timed_out 的判定退出事件）。
        """
        flow_ir, _verdict_target, _timeout_target = self.make_timeout_branch_flow_ir()
        timed_out_facts = JudgeDispatchFacts(
            tokens=0, exit_code=None, timed_out=True, semantic_done=True
        )

        def judge_hook(
            flow_ir_arg, decision_node, evidence_pack, work_dir, packet_dir, timeout_seconds
        ):
            return JudgeVerdict(
                tripped=False,
                chosen_edge_target="T002",
                confidence=0.9,
                evidence=["产物契约齐全"],
                dispatch_facts=timed_out_facts,
            )

        hooks = EngineHooks(
            build_task_packet=self.stubs.build_task_packet,
            probe_agent=self.stubs.probe_agent,
            dispatch_task=self.stubs.dispatch_task,
            judge_decision=judge_hook,
            snapshot=self.stubs.snapshot,
        )
        asyncio.run(run_flow(flow_ir, self.workspace, hooks))

        exit_events = support.events_of_type(self.reopened_ledger(), "agent_exited", "D001")
        self.assertEqual(len(exit_events), 1)
        self.assertTrue(exit_events[0].payload["timed_out"])
        self.assertIsNone(exit_events[0].payload["exit_code"])
        self.assertTrue(exit_events[0].payload["semantic_done"])


if __name__ == "__main__":
    unittest.main()
