"""域D2 · 执行内核测试（tasks.md T010）：task 成功/失败/probe、decision 命中与
tripped、join 等齐、human 暂停与 todo 续跑、fan-in 不重复投递、重放一致性。

直跑：python -m unittest discover -s tests
闸与重试/崩溃恢复类测试见 test_kernel_engine_gates.py。
"""

import asyncio
import tempfile
import unittest
from pathlib import Path

import sys

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))

import kernel_test_support as support  # noqa: E402

from flowrun.ir.schema import (  # noqa: E402
    NODE_KIND_DECISION,
    NODE_KIND_HUMAN,
    NODE_KIND_JOIN,
)
from flowrun.kernel.engine import run_flow  # noqa: E402
from flowrun.kernel.todo_projection import apply_user_marks, write_todo  # noqa: E402
from flowrun.state.ledger import EventLedger  # noqa: E402


class KernelEngineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_engine_test_")
        self.workspace = Path(self._temp_dir.name) / "workspace"
        self.stubs = support.KernelHookStubs()

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def run_flow_once(self, flow_ir) -> object:
        return asyncio.run(run_flow(flow_ir, self.workspace, self.stubs.build_hooks()))

    def reopened_ledger(self) -> EventLedger:
        return EventLedger(self.workspace / "ledger.jsonl")

    # ---------------------------------------------------------------- task 节点

    def test_task_success_end_to_end_with_real_agent_process(self) -> None:
        """真子进程假 agent：packet 注入 → 产物落盘 → usage 记账 → flow_done。"""
        task_node = support.make_node("T001", label="实现登录API", outputs=["src/auth.py"])
        second_task = support.make_node("T002", label="写文档")
        flow_ir = support.make_flow_ir(
            [task_node, second_task],
            [support.make_edge("T001", "T002")],
        )
        subprocess_stubs = support.SubprocessAgentHookStubs({"T001": "normal"})
        self.stubs = subprocess_stubs

        final_state = asyncio.run(
            run_flow(flow_ir, self.workspace, subprocess_stubs.build_hooks())
        )
        self.assertTrue(final_state.is_done())
        self.assertTrue((self.workspace / "src/auth.py").exists())
        self.assertEqual(final_state.node_status("T002"), "done")
        self.assertEqual(final_state.token_spent(), 600,
                         "两次投递各 300 usage，均应进 payload.tokens 累计")
        self.assertTrue(self.stubs.snapshot_calls, "每轮应回调 snapshot hook")

        ledger = self.reopened_ledger()
        self.assertTrue(ledger.verify_chain())
        self.assertEqual(ledger.replay().to_dict(), final_state.to_dict(),
                         "全量重放必须与恢复态一致")

    def test_task_failure_without_retry_trips_retry_gate(self) -> None:
        task_node = support.make_node("T001", outputs=["src/auth.py"])
        second_task = support.make_node("T002")
        flow_ir = support.make_flow_ir(
            [task_node, second_task], [support.make_edge("T001", "T002")]
        )
        self.stubs.script_dispatch_result(
            "T001", support.make_dispatch_result(exit_code=3)
        )
        final_state = self.run_flow_once(flow_ir)
        self.assertTrue(final_state.is_paused())
        self.assertIn("retry", final_state.tripped_gates())
        failed_events = support.events_of_type(self.reopened_ledger(), "node_failed", "T001")
        self.assertEqual(len(failed_events), 1)
        self.assertEqual(failed_events[0].payload["exit_code"], 3)
        self.assertEqual(final_state.node_status("T002"), "planned",
                         "上游失败后下游不得被激活（仍是 planned）")

    def test_probe_unavailable_fails_node_without_dispatch(self) -> None:
        task_node = support.make_node("T001")
        flow_ir = support.make_flow_ir([task_node], [])
        self.stubs.unavailable_agents.add(support.DEFAULT_AGENT_NAME)
        final_state = self.run_flow_once(flow_ir)
        self.assertEqual(self.stubs.dispatch_calls, [], "probe 不可用不得投递")
        self.assertIn(support.DEFAULT_AGENT_NAME, self.stubs.probe_calls)
        failed_events = support.events_of_type(self.reopened_ledger(), "node_failed", "T001")
        self.assertEqual(failed_events[0].payload["reason"], "agent_unavailable")
        self.assertTrue(final_state.is_paused())

    # ---------------------------------------------------------------- decision 节点

    def test_decision_takes_chosen_edge_and_completes(self) -> None:
        upstream_task = support.make_node("T001")
        decision_node = support.make_node("D001", kind=NODE_KIND_DECISION, label="验收?")
        ok_task = support.make_node("T002")
        fail_task = support.make_node("T003")
        flow_ir = support.make_flow_ir(
            [upstream_task, decision_node, ok_task, fail_task],
            [
                support.make_edge("T001", "D001"),
                support.make_edge("D001", "T002", when="ok"),
                support.make_edge("D001", "T003", when="fail"),
            ],
        )
        self.stubs.script_verdict(
            "D001", support.StubJudgeVerdict(chosen_edge_target="T002", confidence=0.92)
        )
        final_state = self.run_flow_once(flow_ir)
        self.assertTrue(final_state.is_done())
        self.assertEqual(final_state.node_status("T002"), "done")
        self.assertEqual(final_state.node_status("T003"), "planned",
                         "未选中的分支不得被激活")
        ledger = self.reopened_ledger()
        verdict_events = support.events_of_type(ledger, "verdict", "D001")
        self.assertEqual(verdict_events[0].payload["edge"], "T002")
        self.assertEqual(verdict_events[0].payload["confidence"], 0.92)
        taken_targets = [
            entry.payload["target"]
            for entry in support.events_of_type(ledger, "edge_taken")
            if entry.payload["source"] == "D001"
        ]
        self.assertEqual(taken_targets, ["T002"])
        # 判定调用也应计入 hop（agent 投递）
        self.assertEqual(
            len(support.events_of_type(ledger, "agent_dispatched", "D001")), 1
        )
        self.assertIn("D001", [call[0] for call in self.stubs.judge_calls])

    def test_decision_tripped_pauses_for_human(self) -> None:
        upstream_task = support.make_node("T001")
        decision_node = support.make_node("D001", kind=NODE_KIND_DECISION)
        downstream_task = support.make_node("T002")
        flow_ir = support.make_flow_ir(
            [upstream_task, decision_node, downstream_task],
            [
                support.make_edge("T001", "D001"),
                support.make_edge("D001", "T002", when="ok"),
            ],
        )
        self.stubs.script_verdict(
            "D001", support.StubJudgeVerdict(tripped=True, confidence=0.3)
        )
        final_state = self.run_flow_once(flow_ir)
        self.assertTrue(final_state.is_paused())
        self.assertIn("verdict", final_state.tripped_gates())
        self.assertEqual(final_state.node_status("T002"), "planned",
                         "verdict tripped 后不得推进任何出边")
        self.assertFalse(final_state.is_done())

    def test_decision_illegal_target_trips_illegal_edge_gate(self) -> None:
        upstream_task = support.make_node("T001")
        decision_node = support.make_node("D001", kind=NODE_KIND_DECISION)
        downstream_task = support.make_node("T002")
        flow_ir = support.make_flow_ir(
            [upstream_task, decision_node, downstream_task],
            [
                support.make_edge("T001", "D001"),
                support.make_edge("D001", "T002", when="ok"),
            ],
        )
        self.stubs.script_verdict(
            "D001", support.StubJudgeVerdict(chosen_edge_target="T999")
        )
        final_state = self.run_flow_once(flow_ir)
        self.assertTrue(final_state.is_paused())
        self.assertIn("illegal_edge", final_state.tripped_gates())
        gate_events = support.events_of_type(self.reopened_ledger(), "gate_tripped")
        self.assertEqual(gate_events[-1].payload["actual"], "T999")

    # ---------------------------------------------------------------- join / human / 一致性

    def test_join_waits_for_all_predecessors(self) -> None:
        entry_task = support.make_node("T001")
        slow_branch = support.make_node("T002")
        join_node = support.make_node("J001", kind=NODE_KIND_JOIN, label="等齐")
        after_join = support.make_node("T003")
        flow_ir = support.make_flow_ir(
            [entry_task, slow_branch, join_node, after_join],
            [
                support.make_edge("T001", "T002"),
                support.make_edge("T001", "J001"),
                support.make_edge("T002", "J001"),
                support.make_edge("J001", "T003", when="all"),
            ],
        )
        final_state = self.run_flow_once(flow_ir)
        self.assertTrue(final_state.is_done())
        ledger = self.reopened_ledger()
        join_done_events = support.events_of_type(ledger, "node_done", "J001")
        self.assertEqual(len(join_done_events), 1)
        self.assertEqual(join_done_events[0].payload["barrier"], ["T001", "T002"])
        t002_done_seq = support.events_of_type(ledger, "node_done", "T002")[0].seq
        self.assertGreater(join_done_events[0].seq, t002_done_seq,
                           "join 必须等齐全部前驱才 node_done")

    def test_human_node_suspends_until_todo_approval(self) -> None:
        task_node = support.make_node("T001")
        human_node = support.make_node("H001", kind=NODE_KIND_HUMAN, label="发布人审")
        release_task = support.make_node("T002")
        flow_ir = support.make_flow_ir(
            [task_node, human_node, release_task],
            [support.make_edge("T001", "H001"), support.make_edge("H001", "T002")],
        )
        suspended_state = self.run_flow_once(flow_ir)
        self.assertEqual(suspended_state.node_status("H001"), "awaiting_human")
        self.assertFalse(suspended_state.is_done())
        self.assertIn("T001", [call[0] for call in self.stubs.dispatch_calls])
        self.assertNotIn("H001", [call[0] for call in self.stubs.dispatch_calls],
                         "人审节点不得投递给 agent")
        self.assertTrue((self.workspace / "todo.md").exists(), "挂起时应写 todo 投影")

        todo_path = self.workspace / "todo.md"
        todo_text = todo_path.read_text(encoding="utf-8")
        todo_path.write_text(
            todo_text.replace("- [ ] H001 发布人审", "- [x] H001 发布人审"),
            encoding="utf-8",
        )
        ledger = self.reopened_ledger()
        applied_count = apply_user_marks(
            flow_ir, todo_path, ledger, ledger.replay()
        )
        self.assertEqual(applied_count, 1)

        resumed_state = self.run_flow_once(flow_ir)
        self.assertTrue(resumed_state.is_done())
        self.assertEqual(resumed_state.node_status("T002"), "done")
        resolved_events = support.events_of_type(
            self.reopened_ledger(), "human_resolved", "H001"
        )
        self.assertEqual(resolved_events[0].payload["decision"], "approve")

    def test_multi_in_edge_decision_node_is_dispatched_only_once(self) -> None:
        """FIX-B1 复现：A→X、A→M、M→X(all)。X 与 M 同轮错峰收尾后，⑤ 激活 M→X
        的 barrier 边时 X 已被投影为 done——旧实现无条件把 target 改回 ready，
        下一轮重复投递（attempt=2）并重复判定。断言：X 只 dispatch 一次/只判一次、
        M 的出边不得落 edge_taken（边未被走，投影只前进不后退）。"""
        entry_task = support.make_node("A001")
        side_branch = support.make_node("M001")
        decision_node = support.make_node("X001", kind=NODE_KIND_DECISION, label="汇总判定")
        after_decision = support.make_node("Y001")
        flow_ir = support.make_flow_ir(
            [entry_task, side_branch, decision_node, after_decision],
            [
                support.make_edge("A001", "X001"),
                support.make_edge("A001", "M001"),
                support.make_edge("M001", "X001", when="all"),
                support.make_edge("X001", "Y001", when="ok"),
            ],
        )
        self.stubs.script_verdict(
            "X001", support.StubJudgeVerdict(chosen_edge_target="Y001")
        )
        final_state = self.run_flow_once(flow_ir)
        self.assertTrue(final_state.is_done())
        ledger = self.reopened_ledger()
        self.assertEqual(
            len(support.events_of_type(ledger, "agent_dispatched", "X001")), 1,
            "多入边节点已 done 后不得被 barrier 边拖回 ready 重复投递",
        )
        self.assertEqual(len(support.events_of_type(ledger, "verdict", "X001")), 1)
        self.assertEqual(len(self.stubs.judge_calls), 1, "判定 hook 只允许调用一次")
        self.assertEqual(
            support.events_of_type(ledger, "edge_taken", "M001"), [],
            "target 已终态时该边不激活（edge_taken 不落——边未被走）",
        )
        self.assertEqual(
            [call[0] for call in self.stubs.dispatch_calls], ["A001", "M001", "Y001"],
            "三个 task 节点各投递恰一次（X001 是判定节点，走 judge 通道，不入 dispatch_calls）",
        )
        self.assertEqual(
            [call[0] for call in self.stubs.judge_calls], ["X001"],
            "判定节点只判一次——四个节点各执行恰一次",
        )
        self.assertEqual(final_state.hop_count(), 4)

    def test_decision_edge_to_already_done_target_keeps_it_done(self) -> None:
        """FIX-B1 投影层复现：判定选中一条指向已 done 节点的边——投影不得把该
        target 回退为 ready 再重跑（旧实现会 attempt=2 重复投递 + 重复 node_done）。"""
        entry_task = support.make_node("A001")
        decision_node = support.make_node("D001", kind=NODE_KIND_DECISION, label="选路")
        target_task = support.make_node("T002", label="已完成任务")
        flow_ir = support.make_flow_ir(
            [entry_task, decision_node, target_task],
            [
                support.make_edge("A001", "D001"),
                support.make_edge("A001", "T002"),
                support.make_edge("D001", "T002", when="ok"),
            ],
        )
        self.stubs.script_verdict(
            "D001", support.StubJudgeVerdict(chosen_edge_target="T002")
        )
        final_state = self.run_flow_once(flow_ir)
        self.assertTrue(final_state.is_done())
        ledger = self.reopened_ledger()
        self.assertEqual(
            len(support.events_of_type(ledger, "agent_dispatched", "T002")), 1,
            "已 done 的 target 不得被 edge_taken 回退 ready 重投",
        )
        self.assertEqual(len(support.events_of_type(ledger, "node_done", "T002")), 1)
        self.assertEqual(final_state.hop_count(), 3)

    def test_fan_in_node_is_dispatched_only_once(self) -> None:
        first_branch = support.make_node("T001")
        second_branch = support.make_node("T002")
        fan_in_task = support.make_node("X001", label="汇总")
        flow_ir = support.make_flow_ir(
            [first_branch, second_branch, fan_in_task],
            [support.make_edge("T001", "X001"), support.make_edge("T002", "X001")],
        )
        final_state = self.run_flow_once(flow_ir)
        self.assertTrue(final_state.is_done())
        dispatch_events = support.events_of_type(
            self.reopened_ledger(), "agent_dispatched", "X001"
        )
        self.assertEqual(len(dispatch_events), 1, "多入边汇入不得重复投递")

    def test_second_run_after_flow_done_adds_no_events(self) -> None:
        task_node = support.make_node("T001")
        flow_ir = support.make_flow_ir([task_node], [])
        self.assertTrue(self.run_flow_once(flow_ir).is_done())
        event_count_after_first = len(self.reopened_ledger().entries())
        second_state = self.run_flow_once(flow_ir)
        self.assertTrue(second_state.is_done())
        self.assertEqual(len(self.reopened_ledger().entries()), event_count_after_first)


if __name__ == "__main__":
    unittest.main()
