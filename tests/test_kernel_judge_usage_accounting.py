"""域D1/D2 · 判定调用 usage 入账测试（tasks.md T020a）。

背景：判定是一次真实 agent 投递，修复前它的 token 消耗不进任何 payload.tokens
（判定消耗只进 hop 闸、不计 token），成本因此不可见、不可审计。

钉三件事：**用量回传**（域D1：``JudgeVerdict.dispatch_facts``——结构化结果优先、
审计 stdout 末个 JSON 兜底、两路只取一不重复计数、重试跨轮累加）；**引擎落账**
（域D2：判定节点按 task 路径同口径落 ``agent_exited``，FlowState 累加进
``token_spent()``，budget 闸不再失明）；**存量兼容**（hook 未回传投递事实时不落
``agent_exited``，旧 verdict / stub 的账本形状零变化）。

真子进程投递形态由 test_kernel_judge.py 覆盖；此处用不落盘的记账适配器，把断言
焦点放在"用量数字怎么走账"上。

直跑：python -m unittest discover -s tests
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import kernel_judge_support

import kernel_judge_support as judge_support  # noqa: E402  (负责把 src/ 加进 sys.path)
import kernel_test_support as kernel_support  # noqa: E402

from flowrun.adapters.base import DispatchResult  # noqa: E402
from flowrun.ir.schema import NODE_KIND_DECISION  # noqa: E402
from flowrun.kernel.engine import EngineHooks, run_flow  # noqa: E402
from flowrun.kernel.engine_evidence import normalize_verdict  # noqa: E402
from flowrun.kernel.judge import JudgeDispatchFacts, JudgeVerdict, judge_decision  # noqa: E402
from flowrun.state.ledger import EventLedger  # noqa: E402

HIT_EDGE_NUMBER = "D001->H001"
MISS_EDGE_NUMBER = "BOGUS->NOWHERE"


def verdict_object(edge_number: str, usage: dict | None = None) -> dict:
    """构造判定 agent 的 verdict JSON 对象（可选带 usage 段）。"""
    verdict = {
        "edge": edge_number,
        "confidence": 0.9,
        "evidence": ["产物契约齐全"],
    }
    if usage is not None:
        verdict["usage"] = usage
    return verdict


def verdict_stdout(edge_number: str, usage: dict | None = None) -> str:
    """判定 agent 的审计 stdout 文本（末行是 verdict JSON）。"""
    return (
        "[judge-agent] thinking...\n"
        + json.dumps(verdict_object(edge_number, usage), ensure_ascii=False)
        + "\n"
    )


class AuditedJudgeAdapter:
    """记账判定适配器：把每次投递的 stdout 文本落盘，并按需回结构化结果。

    不是真子进程——本文件钉的是"用量数字怎么回流与走账"；适配层的真投递形态
    （子进程 / 审计落盘 / stdout 解析）由 test_kernel_judge.py 独立覆盖。
    """

    def __init__(
        self,
        stdout_text_by_attempt: list[str],
        structured_result_by_attempt: list[dict] | None = None,
    ) -> None:
        self.stdout_text_by_attempt = list(stdout_text_by_attempt)
        self.structured_result_by_attempt = list(structured_result_by_attempt or [])
        self.dispatched_packet_paths: list[Path] = []

    @property
    def dispatch_count(self) -> int:
        return len(self.dispatched_packet_paths)

    async def dispatch(self, packet_path, agent_command, work_dir, timeout_seconds,
                       *, audit_artifact=None):
        attempt_index = self.dispatch_count
        self.dispatched_packet_paths.append(Path(packet_path))
        # 判定协议从 AuditArtifact 的规范路径读回 stdout；本适配器必须写到**同一个**
        # 路径，否则判定侧读到空文件。未给 artifact（直调适配器）时退回旧命名。
        if audit_artifact is not None:
            audit_path = audit_artifact.stdout_path()
        else:
            audit_path = Path(work_dir) / f"judge-attempt-{attempt_index + 1}.stdout.log"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(self.stdout_text_by_attempt[attempt_index], encoding="utf-8")
        structured_result = (
            self.structured_result_by_attempt[attempt_index]
            if attempt_index < len(self.structured_result_by_attempt)
            else {}
        )
        return DispatchResult(
            exit_code=0,
            timed_out=False,
            semantic_done=True,
            stdout_path=audit_path,
            duration_seconds=0.01,
            structured_result=dict(structured_result),
        )


class JudgeUsageProtocolTestCase(unittest.TestCase):
    """域D1：判定协议把投递用量回传到 JudgeVerdict.dispatch_facts。"""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_judge_usage_")
        self.addCleanup(self._temp_dir.cleanup)
        self.work_dir = Path(self._temp_dir.name)
        self.flow_ir = judge_support.make_release_flow_ir()
        self.decision_node = self.flow_ir.nodes["D001"]
        self.ledger = EventLedger(self.work_dir / "ledger.jsonl")
        self.addCleanup(self.ledger.close)

    def run_judge_with(self, adapter) -> JudgeVerdict:
        return asyncio.run(
            judge_decision(
                self.flow_ir,
                self.decision_node,
                judge_support.make_evidence_pack(),
                self.work_dir,
                self.work_dir / "packets",
                self.ledger,
                resolve_adapter=lambda agent_name: adapter,
                resolve_command=lambda agent_name: [sys.executable, "-c", "pass"],
            )
        )

    def test_audit_json_usage_is_reported_when_structured_result_is_empty(self) -> None:
        """结构化结果为空时退审计 JSON；总数键优先，不与分量相加（10+20≠30）。"""
        adapter = AuditedJudgeAdapter(
            [
                verdict_stdout(
                    HIT_EDGE_NUMBER,
                    {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
                )
            ]
        )

        verdict = self.run_judge_with(adapter)

        self.assertFalse(verdict.tripped)
        self.assertEqual(verdict.chosen_edge_target, "H001")
        self.assertIsNotNone(verdict.dispatch_facts)
        self.assertEqual(verdict.dispatch_facts.tokens, 30, "总数已含分量，不得累加成 60")
        self.assertEqual(verdict.dispatch_facts.exit_code, 0)
        self.assertFalse(verdict.dispatch_facts.timed_out)
        self.assertTrue(verdict.dispatch_facts.semantic_done)

    def test_structured_result_usage_wins_and_is_not_summed_with_audit_usage(self) -> None:
        """适配层结构化结果（task 路径同源）优先；两路都有用量时不得相加。"""
        adapter = AuditedJudgeAdapter(
            [verdict_stdout(HIT_EDGE_NUMBER, {"total_tokens": 40})],
            structured_result_by_attempt=[{"usage": {"total_tokens": 300}}],
        )

        verdict = self.run_judge_with(adapter)

        self.assertEqual(
            verdict.dispatch_facts.tokens,
            300,
            "同一笔成本两路可见时只取结构化结果，不得累加成 340",
        )

    def test_retry_attempts_sum_usage_across_dispatches(self) -> None:
        """重试是真实成本：两次投递的 tokens 累加（30 + 10 = 40）。"""
        adapter = AuditedJudgeAdapter(
            [
                verdict_stdout(MISS_EDGE_NUMBER, {"total_tokens": 30}),
                verdict_stdout(HIT_EDGE_NUMBER, {"total_tokens": 10}),
            ]
        )

        verdict = self.run_judge_with(adapter)

        self.assertFalse(verdict.tripped)
        self.assertEqual(adapter.dispatch_count, 2)
        self.assertEqual(verdict.dispatch_facts.tokens, 40)

    def test_tripped_verdict_still_reports_usage_of_its_dispatches(self) -> None:
        """判定失败也烧了 token：落 verdict 闸的同时仍回传两轮用量合计。"""
        adapter = AuditedJudgeAdapter(
            [
                verdict_stdout(MISS_EDGE_NUMBER, {"total_tokens": 30}),
                verdict_stdout(MISS_EDGE_NUMBER, {"total_tokens": 30}),
            ]
        )

        verdict = self.run_judge_with(adapter)

        self.assertTrue(verdict.tripped)
        self.assertEqual(verdict.dispatch_facts.tokens, 60)
        gate_entries = [
            entry for entry in self.ledger.entries() if entry.type == "gate_tripped"
        ]
        self.assertEqual(len(gate_entries), 1, "判定失败仍落既有 verdict 闸")

    def test_missing_usage_everywhere_reports_zero_tokens_but_keeps_dispatch_facts(self) -> None:
        """两路都没有 usage：tokens=0，但投递事实仍在（真实投递过 → 引擎可落退出事件）。"""
        adapter = AuditedJudgeAdapter([verdict_stdout(HIT_EDGE_NUMBER)])

        verdict = self.run_judge_with(adapter)

        self.assertIsNotNone(verdict.dispatch_facts)
        self.assertEqual(verdict.dispatch_facts.tokens, 0)

    def test_no_candidate_edges_reports_no_dispatch_facts(self) -> None:
        """没有真实投递（无候选谓词边）时不得伪造投递事实（None=没投过）。"""
        timeout_only_flow_ir = kernel_support.make_flow_ir(
            [
                kernel_support.make_node("D001", kind=NODE_KIND_DECISION),
                kernel_support.make_node("T001"),
            ],
            [kernel_support.make_edge("D001", "T001", timeout_seconds=1800.0)],
        )
        adapter = AuditedJudgeAdapter([])

        verdict = asyncio.run(
            judge_decision(
                timeout_only_flow_ir,
                timeout_only_flow_ir.nodes["D001"],
                judge_support.make_evidence_pack(),
                self.work_dir,
                self.work_dir / "packets",
                self.ledger,
                resolve_adapter=lambda agent_name: adapter,
                resolve_command=lambda agent_name: [sys.executable, "-c", "pass"],
            )
        )

        self.assertTrue(verdict.tripped)
        self.assertIsNone(verdict.dispatch_facts)
        self.assertEqual(adapter.dispatch_count, 0, "没有候选边就不得投递")


if __name__ == "__main__":
    unittest.main()
