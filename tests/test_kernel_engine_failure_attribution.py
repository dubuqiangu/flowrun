"""T020j · 失败归因落账：``node_failed.payload`` 自带真实原因，不再靠相邻事件反推。

直跑：python -m unittest discover -s tests

背景：任务节点的失败分支此前只写 ``{"exit_code":…, "timed_out":…}``——判断"是超时
还是非零退出"必须回到相邻的 ``agent_exited`` 反推。本轮把真实原因**显式**写进
payload：新增 ``reason`` 键，**不改既有键（exit_code / timed_out）的语义、不新增
事件类型**。

覆盖三类原因（其余失败分支本就自带 reason）：
- ``nonzero_exit``：agent 退出码非 0 且未超时；
- ``timed_out``：超时被裁决（exit_code 为 None）；
- ``missing_outputs``：成功退出但契约产物缺失（回归钉子，确保本轮未动其语义）。
"""

import sys
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import kernel_test_support

import kernel_test_support as support  # noqa: E402  (负责把 src/ 加进 sys.path)
from kernel_engine_gates_support import KernelGatesHarnessTestCase  # noqa: E402

SINGLE_TASK_NODE = "T001"
CONTRACT_OUTPUT = "data/input.txt"


class TaskFailureAttributionTest(KernelGatesHarnessTestCase):
    """任务节点失败时 node_failed.payload 必须自带可读的真实原因。"""

    def run_single_task_and_read_failed_payload(
        self, dispatch_result, node_outputs: list[str]
    ) -> dict:
        """跑一次"单任务、无重试边"的流，返回该节点唯一一条 node_failed 的 payload。"""
        flow_ir = support.make_flow_ir(
            nodes=[
                support.make_node(
                    SINGLE_TASK_NODE,
                    agent=support.DEFAULT_AGENT_NAME,
                    outputs=node_outputs,
                )
            ],
            edges=[],
        )
        self.stubs.script_dispatch_result(SINGLE_TASK_NODE, dispatch_result)
        self.run_flow_once(flow_ir)
        failed_events = support.events_of_type(
            self.reopened_ledger(), "node_failed", SINGLE_TASK_NODE
        )
        self.assertEqual(len(failed_events), 1, "单任务失败应恰好落一条 node_failed")
        return failed_events[0].payload

    def test_nonzero_exit_records_reason(self) -> None:
        """非零退出：payload 直接给出 reason=nonzero_exit，无需回看 agent_exited。"""
        failure_payload = self.run_single_task_and_read_failed_payload(
            support.make_dispatch_result(exit_code=3), [CONTRACT_OUTPUT]
        )
        self.assertEqual(failure_payload["reason"], "nonzero_exit")
        self.assertEqual(failure_payload["exit_code"], 3)
        self.assertFalse(failure_payload["timed_out"])

    def test_timed_out_records_reason(self) -> None:
        """超时：reason=timed_out（与 design.md §4.4 的取值一致），exit_code 为 None。"""
        failure_payload = self.run_single_task_and_read_failed_payload(
            support.make_dispatch_result(exit_code=None, timed_out=True), [CONTRACT_OUTPUT]
        )
        self.assertEqual(failure_payload["reason"], "timed_out")
        self.assertIsNone(failure_payload["exit_code"])
        self.assertTrue(failure_payload["timed_out"])

    def test_missing_outputs_reason_is_unchanged(self) -> None:
        """回归钉子：成功退出但产物缺失的原因与字段语义本轮未变（missing 列表仍在）。"""
        failure_payload = self.run_single_task_and_read_failed_payload(
            support.make_dispatch_result(exit_code=0), [CONTRACT_OUTPUT]
        )
        self.assertEqual(failure_payload["reason"], "missing_outputs")
        self.assertEqual(failure_payload["missing"], [CONTRACT_OUTPUT])
        self.assertEqual(failure_payload["exit_code"], 0)
        self.assertNotIn(
            "timed_out", failure_payload, "缺产物分支不写 timed_out（既有语义）"
        )


if __name__ == "__main__":
    unittest.main()
