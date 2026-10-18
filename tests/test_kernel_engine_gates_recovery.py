"""域D2 · 内核恢复与失败路由测试（tasks.md T010）：账本停在 agent_dispatched 的崩溃恢复
（视为中断落 node_failed，retry 边重新入队后 attempt 递增重投直至完成）、
human deny 把节点路由到 retry 闸。

直跑：python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import kernel_engine_gates_support

import kernel_test_support as support  # noqa: E402
from kernel_engine_gates_support import (  # noqa: E402  (负责把 src/ 加进 sys.path)
    KernelGatesHarnessTestCase,
)
from flowrun.ir.schema import NODE_KIND_HUMAN  # noqa: E402
from flowrun.state.ledger import EventLedger  # noqa: E402


class KernelEngineRecoveryTestCase(KernelGatesHarnessTestCase):
    # ---------------------------------------------------------------- 崩溃恢复

    def test_crash_interrupted_running_node_fails_then_recovers(self) -> None:
        """账本停在 agent_dispatched（无 agent_exited）：视为中断落 node_failed，
        retry 边重新入队后 attempt 递增重投直至完成。"""
        flaky_task = support.make_node("T001")
        downstream_task = support.make_node("T002")
        flow_ir = support.make_flow_ir(
            [flaky_task, downstream_task],
            [support.make_edge("T001", "T002", retry=1)],
        )
        crashed_ledger = EventLedger(self.workspace / "ledger.jsonl")
        crashed_ledger.append(
            "flow_started",
            {"entry_nodes": ["T001"], "planned_nodes": ["T001", "T002"]},
            "release-login-flow-started",
            flow_ir.run_name,
        )
        crashed_ledger.append(
            "agent_dispatched",
            {"attempt": 1},
            "release-login-T001-agent-dispatched-1",
            flow_ir.run_name,
            "T001",
        )
        crashed_ledger.close()

        final_state = self.run_flow_once(flow_ir)
        self.assertTrue(final_state.is_done())
        ledger = self.reopened_ledger()
        interrupted_failures = support.events_of_type(ledger, "node_failed", "T001")
        self.assertEqual(len(interrupted_failures), 1)
        self.assertIsNone(interrupted_failures[0].payload["exit_code"])
        self.assertEqual(interrupted_failures[0].payload["reason"], "interrupted_by_restart")
        self.assertEqual(
            [call[0] for call in self.stubs.dispatch_calls], ["T001", "T002"],
            "崩溃前的半次投递不得重复（T001 只以 attempt=2 重投一次，随后正常推进 T002）",
        )
        engine_dispatch_idems = [
            entry.idem
            for entry in support.events_of_type(ledger, "agent_dispatched", "T001")
        ]
        self.assertEqual(len(engine_dispatch_idems), 2)
        self.assertIn("release-login-T001-agent-dispatched-2", engine_dispatch_idems)
        self.assertTrue(ledger.verify_chain())
        self.assertEqual(ledger.replay().to_dict(), final_state.to_dict(),
                         "增量恢复必须与全量重放语义一致")

    # ---------------------------------------------------------------- human deny

    def test_human_deny_routes_node_to_retry_gate(self) -> None:
        task_node = support.make_node("T001")
        human_node = support.make_node("H001", kind=NODE_KIND_HUMAN, label="发布人审")
        release_task = support.make_node("T002")
        flow_ir = support.make_flow_ir(
            [task_node, human_node, release_task],
            [support.make_edge("T001", "H001"), support.make_edge("H001", "T002")],
        )
        first_state = self.run_flow_once(flow_ir)
        self.assertEqual(first_state.node_status("H001"), "awaiting_human")

        deny_ledger = self.reopened_ledger()
        deny_ledger.append(
            "human_resolved",
            {"decision": "deny"},
            "release-login-H001-human-resolved-1",
            flow_ir.run_name,
            "H001",
        )
        deny_ledger.close()

        final_state = self.run_flow_once(flow_ir)
        self.assertEqual(final_state.node_status("H001"), "failed")
        self.assertIn("retry", final_state.tripped_gates())
        self.assertTrue(final_state.is_paused())
        self.assertEqual(final_state.node_status("T002"), "planned")


if __name__ == "__main__":
    unittest.main()
