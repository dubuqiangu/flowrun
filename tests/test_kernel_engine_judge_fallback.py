"""域D2 · 判定路径兜底测试（FIX-B4）：判定 agent 探活失败落 gate_tripped(gate="verdict")
转人审；judge 调用抛异常落 node_failed 终态（原因进 payload["reason"]），由既有失败
处置策略落闸——两种情况都不投递、异常不穿透。

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

    def test_judge_exception_fails_decision_node_without_propagating(self) -> None:
        """缺陷 3：judge 调用抛异常 → 不穿透到调用方；判定节点落到**既有终态** failed，
        失败原因（"异常类型: 消息"）进 node_failed payload["reason"]，再由既有失败处置
        策略落闸转人审。

        旧行为只落 verdict 闸、不落 node_failed：节点停留在非终态，靠下一轮崩溃恢复
        记成含糊的 interrupted_by_restart，判定失败的真实原因丢失且推进不下去。

        说明：这是**纠正前置条件**——旧断言钉住的正是该缺陷行为（判定失败不留失败
        事件），改为钉住"节点必须到既有终态 + 原因留痕"。"""
        self.stubs = _RaisingJudgeStubs()
        flow_ir = self.make_decision_flow()

        final_state = self.run_flow_once(flow_ir)

        self.assertTrue(final_state.is_paused(), "判定失败仍须转人审挂起")
        self.assertEqual(
            final_state.node_status("D001"), "failed", "判定节点必须落到既有终态 failed"
        )
        ledger = self.reopened_ledger()
        failure_events = support.events_of_type(ledger, "node_failed", "D001")
        self.assertEqual(len(failure_events), 1)
        failure_reason = failure_events[0].payload["reason"]
        self.assertIn("FileNotFoundError", failure_reason)
        self.assertIn("可执行文件不存在", failure_reason)
        self.assertEqual(support.events_of_type(ledger, "verdict", "D001"), [])
        self.assertIn("retry", final_state.tripped_gates())
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
