"""域D2 · 判定路径兜底测试（FIX-B4）：判定 agent 探活失败 / judge 调用抛异常时，
落 gate_tripped(gate="verdict") 转人审——不投递、不留悬挂 running、异常不穿透。

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
from flowrun.ir.schema import NODE_KIND_DECISION  # noqa: E402
from flowrun.kernel.engine import run_flow  # noqa: E402
from flowrun.state.ledger import EventLedger  # noqa: E402

AVAILABLE_TASK_AGENT = "available-task-agent"


class _RaisingJudgeStubs(support.KernelHookStubs):
    """judge hook 抛异常的 stub：模拟判定 agent 缺失/spawn 失败（真实现为 WinError 2）。"""

    async def judge_decision(
        self, flow_ir, decision_node, evidence_pack, work_dir, packet_dir, timeout_seconds
    ):
        raise FileNotFoundError("judge-agent: 可执行文件不存在")


class JudgeFallbackTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_judge_fallback_test_")
        self.workspace = Path(self._temp_dir.name) / "workspace"
        self.stubs = support.KernelHookStubs()

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def run_flow_once(self, flow_ir) -> object:
        return asyncio.run(run_flow(flow_ir, self.workspace, self.stubs.build_hooks()))

    def reopened_ledger(self) -> EventLedger:
        return EventLedger(self.workspace / "ledger.jsonl")

    def make_decision_flow(self):
        """上游任务（可用 agent）→ 判定节点 → 收尾，判定 agent 缺省（fake-agent）。"""
        upstream_task = support.make_node("T001", agent=AVAILABLE_TASK_AGENT)
        decision_node = support.make_node("D001", kind=NODE_KIND_DECISION, label="验收?")
        downstream_task = support.make_node("T002")
        return support.make_flow_ir(
            [upstream_task, decision_node, downstream_task],
            [
                support.make_edge("T001", "D001"),
                support.make_edge("D001", "T002", when="ok"),
            ],
        )

    # ---------------------------------------------------------------- probe 兜底

    def test_unavailable_judge_agent_trips_verdict_gate(self) -> None:
        """FIX-B4 复现：判定 agent 不在 PATH → 判定前 probe 拦下，落 verdict 闸，
        不落 agent_dispatched（无悬挂 running）、不调用 judge、异常不穿透。"""
        flow_ir = self.make_decision_flow()
        self.stubs.unavailable_agents.add(support.DEFAULT_AGENT_NAME)

        final_state = self.run_flow_once(flow_ir)

        self.assertIn(
            support.DEFAULT_AGENT_NAME, self.stubs.probe_calls,
            "判定前必须探活判定 agent",
        )
        ledger = self.reopened_ledger()
        self.assertEqual(support.events_of_type(ledger, "agent_dispatched", "D001"), [])
        self.assertEqual(support.events_of_type(ledger, "verdict", "D001"), [])
        self.assertEqual(self.stubs.judge_calls, [])
        gate_events = support.events_of_type(ledger, "gate_tripped", "D001")
        self.assertEqual(gate_events[-1].payload["gate"], "verdict")
        self.assertTrue(final_state.is_paused())
        self.assertNotEqual(
            final_state.node_status("D001"), "running", "不得留下悬挂 running 节点"
        )
        self.assertEqual(final_state.node_status("T002"), "planned")

    # ---------------------------------------------------------------- 异常兜底

    def test_judge_exception_trips_verdict_gate_without_propagating(self) -> None:
        """FIX-B4 复现：judge 调用抛异常 → 不穿透到调用方，落 verdict 闸暂停转人审。"""
        self.stubs = _RaisingJudgeStubs()
        flow_ir = self.make_decision_flow()

        final_state = self.run_flow_once(flow_ir)

        self.assertTrue(final_state.is_paused())
        self.assertIn("verdict", final_state.tripped_gates())
        ledger = self.reopened_ledger()
        gate_events = support.events_of_type(ledger, "gate_tripped", "D001")
        self.assertEqual(gate_events[-1].payload["gate"], "verdict")
        self.assertIn("可执行文件不存在", str(gate_events[-1].payload["actual"]))
        self.assertEqual(support.events_of_type(ledger, "verdict", "D001"), [])
        self.assertEqual(final_state.node_status("T002"), "planned")

    # ---------------------------------------------------------------- 正常路径不受影响

    def test_available_judge_agent_still_takes_chosen_edge(self) -> None:
        """判定 agent 可用时兜底不误伤：verdict 正常落账、选中边正常激活。"""
        flow_ir = self.make_decision_flow()
        self.stubs.script_verdict(
            "D001", support.StubJudgeVerdict(chosen_edge_target="T002")
        )

        final_state = self.run_flow_once(flow_ir)

        self.assertTrue(final_state.is_done())
        self.assertEqual(len(support.events_of_type(self.reopened_ledger(), "verdict", "D001")), 1)
        self.assertEqual(final_state.node_status("T002"), "done")


if __name__ == "__main__":
    unittest.main()
