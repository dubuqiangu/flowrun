"""域D2 · judge 抛异常后判定节点落既有终态（缺陷 3）。

缺陷：判定节点执行 judge 时若判官代码抛异常，节点停在非终态（靠下一轮崩溃恢复
记成含糊的 interrupted_by_restart），失败原因丢失，resume 也推进不下去（死胡同）。

修复：在判定执行边界捕获异常 → 落**既有** ``node_failed``（终态 failed），
``payload["reason"]`` = "异常类型: 消息"（超长截断）；随后由既有失败处置策略
（⑤ resolve_failed_node_policy）落闸转人审。不新造事件类型。

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
from flowrun.kernel.engine_node_executors import (  # noqa: E402
    MAX_JUDGE_FAILURE_REASON_LENGTH,
    describe_judge_failure,
)
from flowrun.state.ledger import EventLedger  # noqa: E402

AVAILABLE_TASK_AGENT = "available-task-agent"


class _RaisingJudgeStubs(support.KernelHookStubs):
    """judge hook 抛异常的 stub（async，异常在 await 处抛出）。"""

    def __init__(self, exception_to_raise: Exception) -> None:
        super().__init__()
        self._exception_to_raise = exception_to_raise

    async def judge_decision(
        self, flow_ir, decision_node, evidence_pack, work_dir, packet_dir, timeout_seconds
    ):
        raise self._exception_to_raise


class _SyncRaisingJudgeStubs(_RaisingJudgeStubs):
    """同步 judge hook 抛异常（非 async）：同样必须被捕获。"""

    def judge_decision(
        self, flow_ir, decision_node, evidence_pack, work_dir, packet_dir, timeout_seconds
    ):
        raise self._exception_to_raise


class JudgeFailureTerminalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_judge_terminal_")
        self.addCleanup(self._temp_dir.cleanup)
        self.workspace = Path(self._temp_dir.name) / "workspace"
        self.stubs = support.KernelHookStubs()

    def run_dir(self) -> Path:
        """本次测试 run 的实际目录（T022：namespaced 布局在 <ws>/<run>/）。"""
        return support.run_dir_for(self.workspace, support.DEFAULT_RUN_NAME)

    def run_flow_once(self, flow_ir) -> object:
        return asyncio.run(run_flow(flow_ir, self.workspace, self.stubs.build_hooks()))

    def reopened_ledger(self) -> EventLedger:
        ledger = EventLedger(self.run_dir() / "ledger.jsonl")
        self.addCleanup(ledger.close)
        return ledger

    def make_decision_flow(self):
        """上游任务（可用 agent）→ 判定节点 → 收尾任务。"""
        return support.make_flow_ir(
            [
                support.make_node("T001", agent=AVAILABLE_TASK_AGENT),
                support.make_node("D001", kind=NODE_KIND_DECISION, label="验收?"),
                support.make_node("T002"),
            ],
            [
                support.make_edge("T001", "D001"),
                support.make_edge("D001", "T002", when="ok"),
            ],
        )

    # ---------------------------------------------------------------- 核心：终态 + 原因留痕

    def test_judge_exception_lands_decision_node_in_failed_terminal(self) -> None:
        """judge 抛异常 → 判定节点落到既有终态 failed（不是非终态），原因进 payload。"""
        self.stubs = _RaisingJudgeStubs(FileNotFoundError("judge-agent: 可执行文件不存在"))
        flow_ir = self.make_decision_flow()

        final_state = self.run_flow_once(flow_ir)  # 异常不得穿透到此

        self.assertEqual(final_state.node_status("D001"), "failed")
        failure_events = support.events_of_type(self.reopened_ledger(), "node_failed", "D001")
        self.assertEqual(len(failure_events), 1, "失败只留一条 node_failed")
        self.assertEqual(
            failure_events[0].payload["reason"],
            "FileNotFoundError: judge-agent: 可执行文件不存在",
            'reason 必须是"异常类型: 消息"',
        )
        # 不得再靠崩溃恢复把原因改写成含糊的 interrupted_by_restart
        self.assertNotIn("interrupted_by_restart", str(failure_events[0].payload["reason"]))

    def test_judge_exception_pauses_for_human_via_existing_gate(self) -> None:
        """异常不得被静默吞掉：既有失败处置策略落闸 → 挂起转人审，下游不推进。"""
        self.stubs = _RaisingJudgeStubs(RuntimeError("judge 崩了"))
        flow_ir = self.make_decision_flow()

        final_state = self.run_flow_once(flow_ir)

        self.assertTrue(final_state.is_paused())
        self.assertEqual(final_state.tripped_gates(), ("retry_exhausted",))
        self.assertEqual(
            [dispatch_call[0] for dispatch_call in self.stubs.dispatch_calls], ["T001"],
            "判定失败后不得投递下游任务节点",
        )
        self.assertEqual(final_state.node_status("T002"), "planned")
        self.assertFalse(final_state.is_done())

    def test_sync_judge_exception_is_also_captured(self) -> None:
        """同步 judge hook 抛异常同样落 node_failed 终态（不穿透、不悬挂）。"""
        self.stubs = _SyncRaisingJudgeStubs(ValueError("同步判定器拒绝执行"))
        flow_ir = self.make_decision_flow()

        final_state = self.run_flow_once(flow_ir)

        self.assertEqual(final_state.node_status("D001"), "failed")
        failure_events = support.events_of_type(self.reopened_ledger(), "node_failed", "D001")
        self.assertEqual(
            failure_events[0].payload["reason"], "ValueError: 同步判定器拒绝执行"
        )

    # ---------------------------------------------------------------- 可处置：resume 不再死胡同

    def test_resume_keeps_failed_node_and_reason_visible(self) -> None:
        """恢复路径（重放账本）必须仍能读出该节点 failed 及其原因；不得静默宣告完成。"""
        self.stubs = _RaisingJudgeStubs(FileNotFoundError("judge-agent 缺失"))
        flow_ir = self.make_decision_flow()
        self.run_flow_once(flow_ir)

        resume_ledger = self.reopened_ledger()
        resume_ledger.append(
            "flow_resumed", {}, f"{flow_ir.run_name}-flow-resumed-1", flow_ir.run_name
        )
        resume_ledger.close()

        final_state = self.run_flow_once(flow_ir)

        self.assertFalse(final_state.is_done(), "失败节点不得被静默当作完成")
        self.assertEqual(final_state.node_status("D001"), "failed")
        replayed_state = self.reopened_ledger().replay()
        self.assertEqual(replayed_state.node_status("D001"), "failed")
        failure_events = support.events_of_type(self.reopened_ledger(), "node_failed", "D001")
        self.assertEqual(len(failure_events), 1, "resume 不得重复落 node_failed")
        self.assertIn("judge-agent 缺失", failure_events[0].payload["reason"])

    # ---------------------------------------------------------------- 原因截断

    def test_long_judge_exception_reason_is_truncated(self) -> None:
        """原因超长必须截断，不把整段堆栈 / 长消息塞进账本。"""
        long_message = "x" * (MAX_JUDGE_FAILURE_REASON_LENGTH * 2)
        self.stubs = _RaisingJudgeStubs(RuntimeError(long_message))
        flow_ir = self.make_decision_flow()

        self.run_flow_once(flow_ir)

        failure_events = support.events_of_type(self.reopened_ledger(), "node_failed", "D001")
        failure_reason = failure_events[0].payload["reason"]
        self.assertLess(len(failure_reason), len(long_message))
        self.assertTrue(failure_reason.startswith("RuntimeError: "))

    def test_describe_judge_failure_formats_type_and_message(self) -> None:
        """原因格式（单元）：'异常类型: 消息'。"""
        self.assertEqual(
            describe_judge_failure(FileNotFoundError("no such agent")),
            "FileNotFoundError: no such agent",
        )
        self.assertEqual(
            describe_judge_failure(ValueError("bad verdict")),
            "ValueError: bad verdict",
        )


if __name__ == "__main__":
    unittest.main()
