"""域D2 · 闸重入语义测试（FIX-B2）：等待点检查先于闸检查（暂停后重跑零新事件）、
「每工作区每闸至多一次」、resume 对既有超限的永久豁免。

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
from flowrun.kernel.todo_projection import append_flow_resumed_event  # noqa: E402
from flowrun.state.ledger import EventLedger  # noqa: E402


class GateExemptionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_gate_exempt_test_")
        # 注册为 cleanup（LIFO）：测试期间注册的账本 close 先执行，之后才删临时目录；
        # 若在 tearDown 里删目录，Windows 上仍打开的 ledger.jsonl 会让 rmtree 失败。
        self.addCleanup(self._temp_dir.cleanup)
        self.workspace = Path(self._temp_dir.name) / "workspace"
        self.stubs = support.KernelHookStubs()

    def run_dir(self) -> Path:
        """本次测试 run 的实际目录（T022：namespaced 布局在 <ws>/<run>/）。"""
        return support.run_dir_for(self.workspace, support.DEFAULT_RUN_NAME)

    def run_flow_once(self, flow_ir) -> object:
        return asyncio.run(run_flow(flow_ir, self.workspace, self.stubs.build_hooks()))

    def reopened_ledger(self) -> EventLedger:
        """重开账本读取落账结果；注册清理，避免测试结束后遗留写句柄（ResourceWarning）。"""
        ledger = EventLedger(self.run_dir() / "ledger.jsonl")
        self.addCleanup(ledger.close)
        return ledger

    def make_budget_overflow_flow(self):
        """两次投递各 10 万 token、预算 15 万：收口前最后一跳烧穿预算 → budget 闸。"""
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
        return flow_ir

    def gate_events_named(self, gate_name: str) -> list:
        """账本中某闸名的全部 gate_tripped 事件。"""
        return [
            entry
            for entry in support.events_of_type(self.reopened_ledger(), "gate_tripped")
            if entry.payload.get("gate") == gate_name
        ]

    # ---------------------------------------------------------------- 等待点先于闸

    def test_already_paused_flow_reruns_without_new_events(self) -> None:
        """FIX-B2：闸触发挂起后直接再 run，不得重复刷账（等待点检查先于闸检查）。"""
        flow_ir = self.make_budget_overflow_flow()
        first_state = self.run_flow_once(flow_ir)
        self.assertTrue(first_state.is_paused())
        self.assertEqual(first_state.tripped_gates(), ("budget",))
        events_after_first_run = len(self.reopened_ledger().entries())

        second_state = self.run_flow_once(flow_ir)

        self.assertTrue(second_state.is_paused())
        self.assertEqual(
            len(self.reopened_ledger().entries()),
            events_after_first_run,
            "已暂停的流再 run 不得产生任何新事件",
        )
        self.assertEqual(second_state.tripped_gates(), ("budget",))
        self.assertEqual(len(self.gate_events_named("budget")), 1)

    def test_user_paused_flow_with_unsettled_gate_adds_no_events(self) -> None:
        """FIX-B2 ②③ 换序：用户指令暂停 + 机器闸已超限但**尚无** gate_tripped →
        再 run 必须先走等待点（零新事件），不得因闸检查在前而替用户补落一条
        gate_tripped。（上面的 budget 闸用例被"每闸至多一次"掩蔽，此例专门钉住换序。）"""
        entry_task = support.make_node("T001")
        flow_ir = support.make_flow_ir([entry_task], [], budget_tokens=1_000)
        seeded_ledger = EventLedger(self.run_dir() / "ledger.jsonl")
        seeded_ledger.append(
            "flow_started",
            {
                "entry_nodes": list(flow_ir.entry_node_ids),
                "planned_nodes": list(flow_ir.nodes),
            },
            f"{flow_ir.run_name}-flow-started",
            flow_ir.run_name,
        )
        seeded_ledger.append(
            "agent_exited",
            {"exit_code": 0, "timed_out": False, "semantic_done": True, "tokens": 5_000},
            f"{flow_ir.run_name}-T001-agent-exited-1",
            flow_ir.run_name,
            "T001",
        )
        seeded_ledger.append(
            "node_done",
            {"artifacts": [], "attempt": 1},
            f"{flow_ir.run_name}-T001-node-done-1",
            flow_ir.run_name,
            "T001",
        )
        seeded_ledger.append(
            "user_directive",
            {"directive": "pause"},
            f"{flow_ir.run_name}-user-directive-pause-1",
            flow_ir.run_name,
        )
        events_before_rerun = len(seeded_ledger.entries())
        seeded_ledger.close()

        final_state = self.run_flow_once(flow_ir)

        self.assertTrue(final_state.is_paused())
        self.assertEqual(
            final_state.tripped_gates(), (),
            "用户主动暂停时不得替其补落机器闸（闸检查必须在等待点之后）",
        )
        self.assertEqual(
            len(self.reopened_ledger().entries()),
            events_before_rerun,
            "用户暂停的流再 run 不得产生任何新事件",
        )

    def test_repeated_runs_do_not_duplicate_retry_gate(self) -> None:
        """FIX-B2：retry 闸触发后反复 run，tripped_gates() 不得变成 ("retry","retry",...)。"""
        failing_task = support.make_node("T001")
        flow_ir = support.make_flow_ir([failing_task], [])
        self.stubs.script_dispatch_result("T001", support.make_dispatch_result(exit_code=3))

        final_state = self.run_flow_once(flow_ir)
        for _repeat in range(2):
            final_state = self.run_flow_once(flow_ir)

        self.assertEqual(final_state.tripped_gates(), ("retry",))
        self.assertEqual(len(self.gate_events_named("retry")), 1)

    # ---------------------------------------------------------------- 每闸至多一次

    def test_parallel_failures_trip_retry_gate_only_once(self) -> None:
        """FIX-B2：同轮两个无重试边的任务各失败 → retry 闸只落一条（每工作区每闸至多一次）。"""
        first_failing_task = support.make_node("T001")
        second_failing_task = support.make_node("T002")
        flow_ir = support.make_flow_ir([first_failing_task, second_failing_task], [])
        self.stubs.script_dispatch_result("T001", support.make_dispatch_result(exit_code=3))
        self.stubs.script_dispatch_result("T002", support.make_dispatch_result(exit_code=3))

        final_state = self.run_flow_once(flow_ir)

        self.assertTrue(final_state.is_paused())
        self.assertEqual(final_state.tripped_gates(), ("retry",))
        self.assertEqual(len(self.gate_events_named("retry")), 1)

    # ---------------------------------------------------------------- resume 永久豁免

    def test_resumed_gate_is_exempted_and_flow_reaches_done(self) -> None:
        """FIX-B2：闸后追加 flow_resumed（人审对既有超限的显式豁免）→ 再 run 不重复
        触发该闸，流程得以收口为 done。"""
        flow_ir = self.make_budget_overflow_flow()
        self.assertTrue(self.run_flow_once(flow_ir).is_paused())

        resume_ledger = self.reopened_ledger()
        append_flow_resumed_event(flow_ir, resume_ledger, by="cli")
        resume_ledger.close()

        final_state = self.run_flow_once(flow_ir)

        self.assertTrue(final_state.is_done(), "被人审豁免后应能推进到 done")
        self.assertFalse(final_state.is_paused())
        self.assertEqual(final_state.tripped_gates(), ("budget",))
        self.assertEqual(
            len(self.gate_events_named("budget")), 1, "豁免后的闸不得再次落账"
        )

    # ---------------------------------------------------------------- K1 机器闸互不遮蔽

    def seed_over_limit_ledger(
        self,
        flow_ir,
        *,
        hop_dispatches: int,
        tokens: int,
        tripped_gates: tuple[str, ...] = (),
        resumed: bool = False,
    ) -> None:
        """手工播种到"已超限"状态：hop 计数由 agent_dispatched 现算，token 由 node_done 现算。"""
        seeded_ledger = EventLedger(self.run_dir() / "ledger.jsonl")
        seeded_ledger.append(
            "flow_started",
            {
                "entry_nodes": list(flow_ir.entry_node_ids),
                "planned_nodes": list(flow_ir.nodes),
            },
            f"{flow_ir.run_name}-flow-started",
            flow_ir.run_name,
        )
        single_task_id = next(iter(flow_ir.nodes))
        for attempt in range(1, hop_dispatches + 1):
            seeded_ledger.append(
                "agent_dispatched",
                {"attempt": attempt},
                f"{flow_ir.run_name}-{single_task_id}-agent-dispatched-{attempt}",
                flow_ir.run_name,
                single_task_id,
            )
        seeded_ledger.append(
            "node_done",
            {"tokens": tokens, "attempt": hop_dispatches},
            f"{flow_ir.run_name}-{single_task_id}-node-done-{hop_dispatches}",
            flow_ir.run_name,
            single_task_id,
        )
        for gate_name in tripped_gates:
            seeded_ledger.append(
                "gate_tripped",
                {"gate": gate_name, "actual": 1, "limit": 0},
                f"{flow_ir.run_name}-gate-{gate_name}-1",
                flow_ir.run_name,
            )
        if resumed:
            seeded_ledger.append(
                "flow_resumed", {}, f"{flow_ir.run_name}-flow-resumed-1", flow_ir.run_name
            )
        seeded_ledger.close()

    def test_budget_gate_trips_even_when_hop_gate_already_settled(self) -> None:
        """K1 复现：hop 永久超限且已被人审 resume 豁免时，budget 超限仍必须落账。

        修复前 check_cycle_gates 早退只返回 hop（hop 优先），hop 一旦了结就让
        budget 永久被遮蔽——预算闸形同不存在。"""
        flow_ir = support.make_flow_ir(
            [support.make_node("T001")], [], budget_tokens=50
        )
        self.seed_over_limit_ledger(
            flow_ir, hop_dispatches=5, tokens=900, tripped_gates=("hop",), resumed=True
        )

        final_state = self.run_flow_once(flow_ir)

        self.assertIn("budget", final_state.tripped_gates())
        budget_events = self.gate_events_named("budget")
        self.assertEqual(len(budget_events), 1)
        self.assertEqual(budget_events[0].payload["limit"], 50)
        self.assertEqual(
            len(self.gate_events_named("hop")), 1, "已了结的 hop 闸不得被重复落账"
        )

    def test_hop_and_budget_gates_both_trip_in_same_round(self) -> None:
        """K1：hop 与 budget 同一轮同时超限 → 两条闸都落账（互不遮蔽）。"""
        flow_ir = support.make_flow_ir(
            [support.make_node("T001")], [], budget_tokens=50
        )
        self.seed_over_limit_ledger(flow_ir, hop_dispatches=5, tokens=900)

        final_state = self.run_flow_once(flow_ir)

        self.assertEqual(final_state.tripped_gates(), ("hop", "budget"))
        self.assertEqual(len(self.gate_events_named("hop")), 1)
        self.assertEqual(len(self.gate_events_named("budget")), 1)

    def test_settled_budget_gate_is_not_re_tripped(self) -> None:
        """K1 一次性语义不变：budget 已了结（且已 resume 豁免）→ 不再落账，流程收口。"""
        flow_ir = support.make_flow_ir(
            [support.make_node("T001")], [], budget_tokens=50
        )
        self.seed_over_limit_ledger(
            flow_ir, hop_dispatches=1, tokens=900, tripped_gates=("budget",), resumed=True
        )

        final_state = self.run_flow_once(flow_ir)

        self.assertTrue(final_state.is_done())
        self.assertEqual(final_state.tripped_gates(), ("budget",))
        self.assertEqual(
            len(self.gate_events_named("budget")), 1, "已了结的 budget 闸不得重复落账"
        )

    def test_resumed_hop_gate_lets_retry_progress_again(self) -> None:
        """FIX-B2：hop 闸触发 → resume 豁免 → 再 run 继续投递（不再被同一闸拦死），
        直到 retry 闸接棒转人审。"""
        storm_task = support.make_node("T001", label="持续失败")
        downstream_task = support.make_node("T002")
        flow_ir = support.make_flow_ir(
            [storm_task, downstream_task],
            [support.make_edge("T001", "T002", retry=9)],
        )
        self.stubs.script_dispatch_result(
            "T001", *(support.make_dispatch_result(exit_code=3) for _ in range(20))
        )
        hop_paused_state = self.run_flow_once(flow_ir)
        self.assertTrue(hop_paused_state.is_paused())
        self.assertEqual(hop_paused_state.tripped_gates(), ("hop",))
        dispatches_before_resume = len(self.stubs.dispatch_calls)

        resume_ledger = self.reopened_ledger()
        append_flow_resumed_event(flow_ir, resume_ledger, by="todo")
        resume_ledger.close()

        final_state = self.run_flow_once(flow_ir)

        self.assertGreater(
            len(self.stubs.dispatch_calls), dispatches_before_resume,
            "hop 闸豁免后再 run 必须继续投递（否则 resume 无法推进）",
        )
        self.assertNotIn("hop", final_state.tripped_gates()[1:], "hop 闸不得重复触发")
        self.assertEqual(len(self.gate_events_named("hop")), 1)
        self.assertIn("retry", final_state.tripped_gates())


if __name__ == "__main__":
    unittest.main()
