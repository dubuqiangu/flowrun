"""域D2 · 判定投递事实的读取口径与引擎侧 usage 入账（tasks.md T020a）。

从 ``test_kernel_judge_usage_accounting`` 拆出（纯结构调整，测试内容零变化）：
那边钉"判定协议如何回传用量"，这边钉"引擎如何按 task 路径同口径落账"，
两边读的是同一份契约，但断言焦点与夹具完全不同。
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import kernel_test_support

import kernel_test_support as kernel_support  # noqa: E402  (负责把 src/ 加进 sys.path)

from flowrun.ir.schema import NODE_KIND_DECISION  # noqa: E402
from flowrun.kernel.engine import EngineHooks, run_flow  # noqa: E402
from flowrun.kernel.engine_evidence import normalize_verdict  # noqa: E402
from flowrun.kernel.judge import JudgeDispatchFacts, JudgeVerdict  # noqa: E402
from flowrun.state.ledger import EventLedger  # noqa: E402


class VerdictDispatchFactsReadingTestCase(unittest.TestCase):
    """域D2：投递事实的两种读法（数据类属性 / dict 键）+ 缺省保守值。"""

    def test_dict_verdict_dispatch_facts_are_read_by_key(self) -> None:
        verdict_view = normalize_verdict(
            {
                "chosen_edge_target": "T002",
                "confidence": 0.9,
                "dispatch_facts": {
                    "tokens": 12,
                    "exit_code": 0,
                    "timed_out": False,
                    "semantic_done": True,
                },
            }
        )

        self.assertEqual(verdict_view.dispatch_facts.tokens, 12)
        self.assertEqual(verdict_view.dispatch_facts.exit_code, 0)

    def test_verdict_without_dispatch_facts_reads_none(self) -> None:
        """旧 verdict / stub 形态：dispatch_facts=None（引擎据此不落 agent_exited）。"""
        verdict_view = normalize_verdict(
            kernel_support.StubJudgeVerdict(chosen_edge_target="T002")
        )

        self.assertIsNone(verdict_view.dispatch_facts)

    def test_bool_tokens_are_not_accounted(self) -> None:
        """bool 不是 token 数（True 不得记成 1），同 extract_usage_tokens 的口径。"""
        verdict_view = normalize_verdict(
            {"chosen_edge_target": "T002", "dispatch_facts": {"tokens": True}}
        )

        self.assertEqual(verdict_view.dispatch_facts.tokens, 0)


class JudgeUsageEngineAccountingTestCase(unittest.TestCase):
    """域D2：判定节点的 agent_exited 入账 + budget 闸可见（T020a 的落账半边）。"""

    JUDGE_TOKENS = 300

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_judge_usage_engine_")
        # 注册为 cleanup（LIFO）：先关账本、后删临时目录（Windows 上仍打开的
        # ledger.jsonl 会让 rmtree 失败）。
        self.addCleanup(self._temp_dir.cleanup)
        self.workspace = Path(self._temp_dir.name) / "workspace"
        self.stubs = kernel_support.KernelHookStubs()

    def make_decision_flow_ir(self, budget_tokens: int = kernel_support.DEFAULT_BUDGET_TOKENS):
        upstream_task = kernel_support.make_node("T001")
        decision_node = kernel_support.make_node("D001", kind=NODE_KIND_DECISION, label="验收?")
        downstream_task = kernel_support.make_node("T002")
        return kernel_support.make_flow_ir(
            [upstream_task, decision_node, downstream_task],
            [
                kernel_support.make_edge("T001", "D001"),
                kernel_support.make_edge("D001", "T002", when="ok"),
            ],
            budget_tokens=budget_tokens,
        )

    def run_flow_with_judge_verdict(self, judge_verdict: JudgeVerdict, flow_ir):
        def judge_hook(
            flow_ir_arg, decision_node, evidence_pack, work_dir, packet_dir, timeout_seconds
        ) -> JudgeVerdict:
            return judge_verdict

        hooks = EngineHooks(
            build_task_packet=self.stubs.build_task_packet,
            probe_agent=self.stubs.probe_agent,
            dispatch_task=self.stubs.dispatch_task,
            judge_decision=judge_hook,
            snapshot=self.stubs.snapshot,
        )
        return asyncio.run(run_flow(flow_ir, self.workspace, hooks))

    def reopened_ledger(self) -> EventLedger:
        ledger = EventLedger(self.workspace / "ledger.jsonl")
        self.addCleanup(ledger.close)
        return ledger

    def judge_verdict_with_tokens(self, tokens: int) -> JudgeVerdict:
        return JudgeVerdict(
            tripped=False,
            chosen_edge_target="T002",
            confidence=0.9,
            evidence=["产物契约齐全"],
            dispatch_facts=JudgeDispatchFacts(
                tokens=tokens, exit_code=0, timed_out=False, semantic_done=True
            ),
        )

    def test_judge_tokens_land_in_agent_exited_and_token_spent(self) -> None:
        """判定节点落一条带 tokens 的 agent_exited，并计入 token_spent()。"""
        final_state = self.run_flow_with_judge_verdict(
            self.judge_verdict_with_tokens(self.JUDGE_TOKENS), self.make_decision_flow_ir()
        )

        exited_events = kernel_support.events_of_type(
            self.reopened_ledger(), "agent_exited", "D001"
        )
        self.assertEqual(len(exited_events), 1, "判定恰好投递一次 → 恰一条退出事件")
        self.assertEqual(exited_events[0].payload["tokens"], self.JUDGE_TOKENS)
        self.assertEqual(exited_events[0].payload["exit_code"], 0)
        self.assertEqual(exited_events[0].payload["semantic_done"], True)
        self.assertEqual(
            final_state.token_spent(), self.JUDGE_TOKENS, "判定消耗必须进 token 账"
        )

    def test_agent_exited_precedes_verdict_for_decision_node(self) -> None:
        """落账顺序与 task 路径一致：agent_dispatched → agent_exited → verdict。"""
        self.run_flow_with_judge_verdict(
            self.judge_verdict_with_tokens(self.JUDGE_TOKENS), self.make_decision_flow_ir()
        )

        event_types_for_decision = [
            entry.type
            for entry in self.reopened_ledger().entries()
            if entry.node == "D001"
        ]
        self.assertEqual(
            event_types_for_decision, ["agent_dispatched", "agent_exited", "verdict", "edge_taken"]
        )

    def test_judge_tokens_can_trip_budget_gate(self) -> None:
        """判定成本对 budget 闸可见：判定烧穿预算 → 落 budget 闸、不得宣告 flow_done。"""
        final_state = self.run_flow_with_judge_verdict(
            self.judge_verdict_with_tokens(self.JUDGE_TOKENS),
            self.make_decision_flow_ir(budget_tokens=100),
        )

        budget_gate_entries = [
            entry
            for entry in kernel_support.events_of_type(self.reopened_ledger(), "gate_tripped")
            if entry.payload.get("gate") == "budget"
        ]
        self.assertEqual(len(budget_gate_entries), 1, "判定消耗必须能被 budget 闸看到")
        self.assertEqual(budget_gate_entries[0].payload["actual"], self.JUDGE_TOKENS)
        self.assertFalse(final_state.is_done(), "预算超限不得收口")

    def test_verdict_without_dispatch_facts_adds_no_agent_exited(self) -> None:
        """存量兼容：hook 未回传投递事实时不落 agent_exited（stub 账本形状零变化）。"""
        stub_verdict = JudgeVerdict(
            tripped=False, chosen_edge_target="T002", confidence=0.9, evidence=["ok"]
        )

        final_state = self.run_flow_with_judge_verdict(
            stub_verdict, self.make_decision_flow_ir()
        )

        self.assertEqual(
            kernel_support.events_of_type(self.reopened_ledger(), "agent_exited", "D001"),
            [],
        )
        self.assertEqual(final_state.token_spent(), 0)


if __name__ == "__main__":
    unittest.main()
