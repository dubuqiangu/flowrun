"""kernel judge 测试（域D1 · T009）：假判定 agent 直跑完整判定协议。

覆盖：命中 / 不命中重试 / 低 confidence 重试 / gate_tripped / 审计无 JSON /
最后一个 JSON 提取 / timeout 与 barrier 边排除 / 默认 agent 回退 /
适配器不可用 / gate idem attempt 递增。全部 stdlib unittest，零配置直跑。

纯函数（is_predicate_edge / extract_last_verdict）见
test_kernel_judge_verdict_extraction.py。
"""
import asyncio
import sys
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import kernel_judge_support

from kernel_judge_support import (  # noqa: E402  (负责把 src/ 加进 sys.path)
    JudgeDecisionTestBase,
    make_evidence_pack,
)
from flowrun.ir.schema import EdgeSpec, NodeSpec  # noqa: E402
from flowrun.kernel.judge import (  # noqa: E402
    VERDICT_RETRY_LIMIT,
    judge_decision,
)


class VerdictHitTest(JudgeDecisionTestBase):
    def test_hit_returns_chosen_edge_target_without_ledger_writes(self):
        verdict = self.run_judge("hit")
        self.assertFalse(verdict.tripped)
        self.assertEqual(verdict.chosen_edge_target, "H001")  # 候选 1 号边 D001->H001
        self.assertEqual(verdict.confidence, 0.9)
        self.assertEqual(verdict.evidence, ["产物契约齐全", "退出码为 0"])
        self.assertEqual(self.adapter.dispatch_count, 1)
        # 成功不落账（verdict / edge_taken 由 engine 落）
        self.assertEqual(self.ledger.entries(), [])
        # 判定 packet 已落盘
        self.assertTrue((self.packet_dir / "D001.judge.md").exists())

    def test_last_json_object_wins_even_after_earlier_miss_json(self):
        verdict = self.run_judge("two-json")
        self.assertFalse(verdict.tripped)
        self.assertEqual(verdict.chosen_edge_target, "H001")
        self.assertEqual(verdict.confidence, 0.85)


class VerdictRetryTest(JudgeDecisionTestBase):
    def test_miss_once_then_hit_succeeds_on_retry(self):
        verdict = self.run_judge("flaky-miss-then-hit")
        self.assertFalse(verdict.tripped)
        self.assertEqual(verdict.chosen_edge_target, "H001")
        self.assertEqual(self.adapter.dispatch_count, 1 + VERDICT_RETRY_LIMIT)
        self.assertEqual(self.ledger.entries(), [])

    def test_low_confidence_once_then_hit_succeeds_on_retry(self):
        verdict = self.run_judge("flaky-low-confidence-then-hit")
        self.assertFalse(verdict.tripped)
        self.assertEqual(verdict.chosen_edge_target, "H001")
        self.assertEqual(self.adapter.dispatch_count, 1 + VERDICT_RETRY_LIMIT)
        self.assertEqual(self.ledger.entries(), [])


class GateTrippedTest(JudgeDecisionTestBase):
    def test_persistent_miss_trips_verdict_gate(self):
        verdict = self.run_judge("miss")
        self.assertTrue(verdict.tripped)
        self.assertIsNone(verdict.chosen_edge_target)
        self.assertEqual(self.adapter.dispatch_count, 1 + VERDICT_RETRY_LIMIT)
        self.assert_single_gate_tripped("未命中候选边编号集合")
        self.assertIn("未命中候选边编号集合", verdict.evidence[0])

    def test_persistent_low_confidence_trips_verdict_gate(self):
        verdict = self.run_judge("low-confidence")
        self.assertTrue(verdict.tripped)
        self.assertEqual(self.adapter.dispatch_count, 1 + VERDICT_RETRY_LIMIT)
        self.assert_single_gate_tripped("confidence 0.3")

    def test_audit_without_json_trips_verdict_gate(self):
        verdict = self.run_judge("no-json")
        self.assertTrue(verdict.tripped)
        self.assertEqual(self.adapter.dispatch_count, 1 + VERDICT_RETRY_LIMIT)
        self.assert_single_gate_tripped("未找到可解析的 JSON verdict")

    def test_unresolvable_adapter_trips_gate_without_dispatch(self):
        self.judge_mode = "hit"

        def resolve_adapter_returning_none(agent_name):
            self.resolved_agent_names.append(agent_name)
            return None

        verdict = asyncio.run(
            judge_decision(
                self.flow_ir,
                self.decision_node,
                make_evidence_pack(),
                self.work_dir,
                self.packet_dir,
                self.ledger,
                resolve_adapter=resolve_adapter_returning_none,
                resolve_command=self.resolve_command,
            )
        )
        self.assertTrue(verdict.tripped)
        self.assertEqual(self.adapter.dispatch_count, 0)  # 没有适配器，不投递
        self.assert_single_gate_tripped("没有可用适配器")

    def test_empty_command_template_trips_gate(self):
        self.judge_mode = "hit"
        verdict = asyncio.run(
            judge_decision(
                self.flow_ir,
                self.decision_node,
                make_evidence_pack(),
                self.work_dir,
                self.packet_dir,
                self.ledger,
                resolve_adapter=self.resolve_adapter,
                resolve_command=lambda agent_name: [],
            )
        )
        self.assertTrue(verdict.tripped)
        self.assertEqual(self.adapter.dispatch_count, 0)
        self.assert_single_gate_tripped("命令模板为空")

    def test_gate_tripped_idem_attempt_increments_across_calls(self):
        first_verdict = self.run_judge("miss")
        second_verdict = self.run_judge("miss")
        self.assertTrue(first_verdict.tripped)
        self.assertTrue(second_verdict.tripped)
        gate_entries = self.gate_tripped_entries()
        self.assertEqual(len(gate_entries), 2)
        self.assertNotEqual(gate_entries[0].idem, gate_entries[1].idem)
        self.assertIn("-a1", gate_entries[0].idem)
        self.assertIn("-a2", gate_entries[1].idem)


class AgentResolutionTest(JudgeDecisionTestBase):
    def test_decision_node_without_agent_falls_back_to_default_agent(self):
        self.run_judge("hit")
        self.assertEqual(self.resolved_agent_names, ["claude-code"])

    def test_decision_node_agent_overrides_default(self):
        self.decision_node.agent = "opencode"
        self.run_judge("hit")
        self.assertEqual(self.resolved_agent_names, ["opencode"])


class CandidateEdgeFilteringTest(JudgeDecisionTestBase):
    def test_timeout_and_barrier_edges_excluded_from_judge_packet(self):
        # 给 D001 加一条 when:all 出边（畸形但用于验证过滤）与已有 timeout 边
        barrier_target = NodeSpec(
            node_id="J001",
            kind="join",
            label="汇合点",
            agent=None,
            outputs=[],
            line_number=10,
        )
        self.flow_ir.nodes["J001"] = barrier_target
        self.flow_ir.edges.append(EdgeSpec("D001", "J001", "all", 0, None, 11))

        self.run_judge("hit")
        packet_text = (self.packet_dir / "D001.judge.md").read_text(encoding="utf-8")
        self.assertIn("D001->H001", packet_text)   # 谓词边 ok 在候选
        self.assertIn("D001->T002", packet_text)   # 谓词边 fail 在候选
        self.assertNotIn("D001->T003", packet_text)  # timeout 边不进候选
        self.assertNotIn("D001->J001", packet_text)   # when:all barrier 边不进候选

    def test_decision_node_without_predicate_edges_trips_gate(self):
        # 删掉两条谓词边，只剩 timeout 边 → 没有候选 → gate_tripped
        self.flow_ir.edges = [
            edge for edge in self.flow_ir.edges if edge.when is None
        ]
        verdict = self.run_judge("hit")
        self.assertTrue(verdict.tripped)
        self.assertEqual(self.adapter.dispatch_count, 0)
        self.assert_single_gate_tripped("没有候选谓词边", expected_limit_fragment="[]")


if __name__ == "__main__":
    unittest.main()
