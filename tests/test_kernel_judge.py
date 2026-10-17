"""kernel judge 测试（域D1 · T009）：假判定 agent 直跑完整判定协议。

覆盖：命中 / 不命中重试 / 低 confidence 重试 / gate_tripped / 审计无 JSON /
最后一个 JSON 提取 / timeout 与 barrier 边排除 / 默认 agent 回退 /
适配器不可用 / gate idem attempt 递增。全部 stdlib unittest，零配置直跑。
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from flowrun.ir.schema import (  # noqa: E402
    NODE_KIND_DECISION,
    NODE_KIND_TASK,
    AgentCommandSpec,
    EdgeSpec,
    FlowIR,
    NodeSpec,
)
from flowrun.kernel.fallback_adapter import FallbackAdapter  # noqa: E402
from flowrun.kernel.judge import (  # noqa: E402
    VERDICT_RETRY_LIMIT,
    EvidencePack,
    extract_last_verdict,
    is_predicate_edge,
    judge_decision,
)
from flowrun.state.ledger import EventLedger  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
FAKE_JUDGE_AGENT_SCRIPT = FIXTURES_DIR / "fake_judge_agent.py"


class CountingFallbackAdapter(FallbackAdapter):
    """记录投递次数的兜底适配器（判定走真实子进程 + 审计落盘路径）。"""

    def __init__(self):
        super().__init__()
        self.dispatch_count = 0
        self.dispatched_packet_paths: list[Path] = []

    async def dispatch(self, packet_path, agent_command, work_dir, timeout_seconds):
        self.dispatch_count += 1
        self.dispatched_packet_paths.append(Path(packet_path))
        return await super().dispatch(packet_path, agent_command, work_dir, timeout_seconds)


def make_release_flow_ir() -> FlowIR:
    """样例判定流：T001 -> D001，D001 三路出边（ok / fail+retry / timeout）。"""
    return FlowIR(
        run_name="release-login",
        default_agent="claude-code",
        budget_tokens=200000,
        agent_registry={
            "claude-code": AgentCommandSpec(["claude", "-p"]),
            "opencode": AgentCommandSpec(["opencode", "run"]),
        },
        nodes={
            "T001": NodeSpec(
                node_id="T001",
                kind=NODE_KIND_TASK,
                label="实现登录API",
                agent="claude-code",
                outputs=["src/auth.py"],
                line_number=1,
            ),
            "T002": NodeSpec(
                node_id="T002",
                kind=NODE_KIND_TASK,
                label="修复登录缺陷",
                agent="opencode",
                outputs=["src/fix.patch"],
                line_number=2,
            ),
            "T003": NodeSpec(
                node_id="T003",
                kind=NODE_KIND_TASK,
                label="超时诊断",
                agent="claude-code",
                outputs=[],
                line_number=3,
            ),
            "D001": NodeSpec(
                node_id="D001",
                kind=NODE_KIND_DECISION,
                label="验收通过?",
                agent=None,  # None → 用 default_agent
                outputs=[],
                line_number=4,
            ),
            "H001": NodeSpec(
                node_id="H001",
                kind="human",
                label="发布人审",
                agent=None,
                outputs=[],
                line_number=5,
            ),
        },
        edges=[
            EdgeSpec("T001", "D001", None, 0, None, 6),
            EdgeSpec("D001", "H001", "ok", 0, None, 7),
            EdgeSpec("D001", "T002", "fail", 2, None, 8),
            EdgeSpec("D001", "T003", None, 0, 1800.0, 9),  # timeout 边
        ],
        entry_node_ids=["T001"],
    )


def make_evidence_pack() -> EvidencePack:
    return EvidencePack(
        node_id="T001",
        exit_code=0,
        timed_out=False,
        semantic_done=False,
        duration_seconds=1.5,
        outputs_present={"src/auth.py": True},
        audit_tail="全部测试通过\nDONE",
    )


class JudgeDecisionTestBase(unittest.TestCase):
    """每个用例独立的 work_dir / packet_dir / ledger / 计数适配器。"""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun-judge-test-")
        self.work_dir = Path(self._temp_dir.name)
        self.packet_dir = self.work_dir / "packets"
        self.ledger = EventLedger(self.work_dir / "ledger.jsonl")
        self.flow_ir = make_release_flow_ir()
        self.decision_node = self.flow_ir.nodes["D001"]
        self.adapter = CountingFallbackAdapter()
        self.resolved_agent_names: list[str] = []

    def tearDown(self):
        self.ledger.close()
        self._temp_dir.cleanup()

    def resolve_adapter(self, agent_name):
        self.resolved_agent_names.append(agent_name)
        return self.adapter

    def resolve_command(self, agent_name):
        return [sys.executable, str(FAKE_JUDGE_AGENT_SCRIPT), "--mode", self.judge_mode]

    def run_judge(self, judge_mode: str):
        self.judge_mode = judge_mode
        return asyncio.run(
            judge_decision(
                self.flow_ir,
                self.decision_node,
                make_evidence_pack(),
                self.work_dir,
                self.packet_dir,
                self.ledger,
                resolve_adapter=self.resolve_adapter,
                resolve_command=self.resolve_command,
            )
        )

    def gate_tripped_entries(self):
        return [
            entry for entry in self.ledger.entries() if entry.type == "gate_tripped"
        ]

    def assert_single_gate_tripped(
        self, expected_reason_fragment: str, expected_limit_fragment: str = "D001->H001"
    ):
        gate_entries = self.gate_tripped_entries()
        self.assertEqual(len(gate_entries), 1)
        gate_entry = gate_entries[0]
        self.assertEqual(gate_entry.node, "D001")
        self.assertEqual(gate_entry.payload.get("gate"), "verdict")
        self.assertIn(expected_reason_fragment, gate_entry.payload.get("actual", ""))
        self.assertIn(expected_limit_fragment, gate_entry.payload.get("limit", ""))


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


class IsPredicateEdgeTest(unittest.TestCase):
    def test_natural_language_when_is_predicate(self):
        self.assertTrue(is_predicate_edge(EdgeSpec("D001", "H001", "ok", 0, None, 1)))

    def test_unconditional_edge_is_not_predicate(self):
        self.assertFalse(is_predicate_edge(EdgeSpec("T001", "D001", None, 0, None, 1)))

    def test_barrier_edge_is_not_predicate(self):
        self.assertFalse(is_predicate_edge(EdgeSpec("J001", "D001", "all", 0, None, 1)))

    def test_timeout_edge_is_not_predicate(self):
        self.assertFalse(is_predicate_edge(EdgeSpec("D001", "T003", None, 0, 1800.0, 1)))

    def test_malformed_when_with_timeout_is_not_predicate(self):
        self.assertFalse(is_predicate_edge(EdgeSpec("D001", "T003", "ok", 0, 1800.0, 1)))


class ExtractLastVerdictTest(unittest.TestCase):
    def test_takes_the_last_parseable_json_object(self):
        stdout_text = (
            '思考中...\n{"edge":"BOGUS->EARLIER","confidence":0.95,"evidence":[]}\n'
            '结论如下\n{"edge":"D001->H001","confidence":0.8,"evidence":["契约齐全"]}\n'
        )
        verdict = extract_last_verdict(stdout_text)
        self.assertEqual(verdict["edge"], "D001->H001")
        self.assertEqual(verdict["confidence"], 0.8)
        self.assertEqual(verdict["evidence"], ["契约齐全"])

    def test_returns_none_for_plain_text(self):
        self.assertIsNone(extract_last_verdict("没有任何结构化输出。\n"))

    def test_rejects_non_numeric_confidence(self):
        self.assertIsNone(
            extract_last_verdict('{"edge":"D001->H001","confidence":"high"}')
        )

    def test_rejects_boolean_confidence(self):
        self.assertIsNone(
            extract_last_verdict('{"edge":"D001->H001","confidence":true}')
        )

    def test_missing_evidence_defaults_to_empty_list(self):
        verdict = extract_last_verdict('{"edge":"D001->H001","confidence":0.7}')
        self.assertEqual(verdict["evidence"], [])

    # ---- K4：字符串上下文内的花括号不得破坏平衡扫描

    def test_closing_brace_inside_string_does_not_break_extraction(self):
        stdout_text = '{"edge":"D001->H001","confidence":0.9,"evidence":["a } b"]}'
        verdict = extract_last_verdict(stdout_text)
        self.assertIsNotNone(verdict, "字符串内的 } 是合法 JSON，必须仍能提取判定")
        self.assertEqual(verdict["edge"], "D001->H001")
        self.assertEqual(verdict["evidence"], ["a } b"])

    def test_opening_brace_inside_string_does_not_break_extraction(self):
        stdout_text = '结论\n{"edge":"D001->H001","confidence":0.9,"evidence":["a { b"]}\n'
        verdict = extract_last_verdict(stdout_text)
        self.assertIsNotNone(verdict, "字符串内的 { 是合法 JSON，必须仍能提取判定")
        self.assertEqual(verdict["edge"], "D001->H001")
        self.assertEqual(verdict["evidence"], ["a { b"])

    def test_escaped_quote_inside_string_does_not_break_extraction(self):
        stdout_text = '{"edge":"D001->H001","confidence":0.9,"evidence":["say \\"{\\""]}'
        verdict = extract_last_verdict(stdout_text)
        self.assertIsNotNone(verdict, "转义引号后的花括号仍在字符串内，不得被当作结构括号")
        self.assertEqual(verdict["edge"], "D001->H001")
        self.assertEqual(verdict["evidence"], ['say "{"'])

    def test_nested_object_is_extracted_as_one_candidate(self):
        stdout_text = (
            '{"edge":"D001->H001","confidence":0.9,"evidence":[],'
            '"extra":{"nested":{"deep":true}}}'
        )
        verdict = extract_last_verdict(stdout_text)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["edge"], "D001->H001")

    def test_last_object_wins_when_earlier_object_has_brace_in_string(self):
        stdout_text = (
            '{"edge":"OLD->OLD","confidence":0.9,"evidence":["}"]}\n'
            '{"edge":"D001->H001","confidence":0.8,"evidence":[]}\n'
        )
        verdict = extract_last_verdict(stdout_text)
        self.assertEqual(verdict["edge"], "D001->H001")
        self.assertEqual(verdict["confidence"], 0.8)


if __name__ == "__main__":
    unittest.main()
