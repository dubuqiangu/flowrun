"""T020b / T020m / T020d 验收：审计产物归位、命名账本可推导、账本单实例。

修复前：派发审计落**工作区根**目录，文件名带时间戳与进程内序号，读侧只能靠
本进程内的 ``DispatchResult.stdout_path.name`` 找回 → 跨进程/恢复后 ``audit_tail``
恒空（证据丢失）。本文件钉住三条硬条件，缺一不算完成：

1. **归位（T020b）**——任何派发审计产物（任务/判定/无适配器防御分支）都落
   ``workspace/audit/``，工作区根不再出现 ``*.stdout.log`` / ``*.stderr.log``；
2. **命名可推导（T020m）**——文件名是**账本可推导的纯函数**
   ``<node_id>-<kind>-<attempt>.stdout.log``；新实例仅凭账本条目即可算出相对路径
   并读回**非空** audit_tail（证明读侧不再依赖进程内状态）；同 attempt 重投覆盖
   同名文件，不产生第二个名字；
3. **单实例（T020d）**——``run_flow`` **借用**传入账本时全程不 close（调用方持有）。

直跑：python -m unittest discover -s tests
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import kernel_test_support

import kernel_test_support as support  # noqa: E402  (负责把 src/ 加进 sys.path)

from flowrun.adapters.base import (  # noqa: E402
    AUDIT_KIND_AGENT,
    AuditArtifact,
)
from flowrun.ir.schema import (  # noqa: E402
    NODE_KIND_DECISION,
    AgentCommandSpec,
    FlowIR,
)
from flowrun.kernel.engine import run_flow  # noqa: E402
from flowrun.kernel.engine_evidence import audit_relative_path_for_node  # noqa: E402
from flowrun.kernel.evidence_conversion import _read_audit_tail  # noqa: E402
from flowrun.kernel.fallback_adapter import FallbackAdapter  # noqa: E402
from flowrun.kernel.hooks_assembly import (  # noqa: E402
    _unavailable_agent_result,
    build_engine_hooks,
)
from flowrun.state.ledger import EventLedger  # noqa: E402

TASK_AGENT_NAME = "task-agent"
JUDGE_AGENT_NAME = "judge-agent"
FIRST_REPORT_MARKER = "audit-marker-from-first-dispatch"
SECOND_REPORT_MARKER = "audit-marker-from-second-dispatch"
TASK_AUDIT_MARKER = "audit-marker-from-task-agent"
JUDGE_AUDIT_MARKER = "audit-marker-from-judge-agent"
DECISION_VERDICT_EDGE = "D001->T002"


def write_reporting_script(script_path: Path, marker: str) -> None:
    """写一个"打印标记行后退出 0"的最小假 agent CLI。"""
    script_path.write_text(
        "import sys\n"
        'sys.stdout.reconfigure(encoding="utf-8")\n'
        f"print({marker!r}, flush=True)\n",
        encoding="utf-8",
    )


def write_judge_script(script_path: Path, marker: str, verdict_edge: str) -> None:
    """写一个"打印标记行 + 一个合法 verdict JSON 后退出 0"的最小假判定 agent。"""
    verdict_text = (
        '{"edge": "%s", "confidence": 0.9, "evidence": ["judged"]}' % verdict_edge
    )
    script_path.write_text(
        "import sys\n"
        'sys.stdout.reconfigure(encoding="utf-8")\n'
        f"print({marker!r}, flush=True)\n"
        f"print({verdict_text!r}, flush=True)\n",
        encoding="utf-8",
    )


def make_task_flow(agent_command: list[str]) -> FlowIR:
    """单任务节点流程：T001（agent 命令由调用方给定）。"""
    return FlowIR(
        run_name="audit-layout",
        default_agent=TASK_AGENT_NAME,
        budget_tokens=support.DEFAULT_BUDGET_TOKENS,
        agent_registry={TASK_AGENT_NAME: AgentCommandSpec(list(agent_command))},
        nodes={"T001": support.make_node("T001")},
        edges=[],
        entry_node_ids=["T001"],
    )


def make_decision_flow(task_command: list[str], judge_command: list[str]) -> FlowIR:
    """T001 → D001（判定）→ T002：任务与判定两条审计路径都经过真实投递。"""
    return FlowIR(
        run_name="audit-layout",
        default_agent=TASK_AGENT_NAME,
        budget_tokens=support.DEFAULT_BUDGET_TOKENS,
        agent_registry={
            TASK_AGENT_NAME: AgentCommandSpec(list(task_command)),
            JUDGE_AGENT_NAME: AgentCommandSpec(list(judge_command)),
        },
        nodes={
            "T001": support.make_node("T001", agent=TASK_AGENT_NAME),
            "D001": support.make_node("D001", kind=NODE_KIND_DECISION, agent=JUDGE_AGENT_NAME),
            "T002": support.make_node("T002", agent=TASK_AGENT_NAME),
        },
        edges=[
            support.make_edge("T001", "D001"),
            support.make_edge("D001", "T002", when="判定通过"),
        ],
        entry_node_ids=["T001"],
    )


class AuditArtifactLayoutTestCase(unittest.TestCase):
    """端到端（真子进程）：审计产物的位置、命名与账本可推导性。"""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_audit_layout_")
        self.addCleanup(self._temp_dir.cleanup)
        self.workspace = Path(self._temp_dir.name)
        self.task_script = self.workspace / "fake_task_agent.py"
        self.judge_script = self.workspace / "fake_judge_agent.py"
        write_reporting_script(self.task_script, TASK_AUDIT_MARKER)
        write_judge_script(self.judge_script, JUDGE_AUDIT_MARKER, DECISION_VERDICT_EDGE)

    def audit_directory(self) -> Path:
        return self.workspace / "audit"

    def audit_stdout_names(self) -> list[str]:
        return sorted(path.name for path in self.audit_directory().glob("*.stdout.log"))

    def run_with_borrowed_ledger(self, flow_ir: FlowIR) -> tuple[EventLedger, object]:
        """按生产装配方式跑一遍：同一个账本实例既喂 hooks 也喂内核。"""
        ledger = EventLedger(self.workspace / "ledger.jsonl")
        self.addCleanup(ledger.close)
        engine_hooks = build_engine_hooks(flow_ir, self.workspace, ledger, 60.0)
        flow_state = asyncio.run(run_flow(flow_ir, self.workspace, engine_hooks, ledger))
        return ledger, flow_state

    def fresh_readonly_ledger(self) -> EventLedger:
        """等价"新进程"：只读视图（不截断尾行、不持写句柄，无任何进程内缓存）。"""
        return EventLedger.open_readonly(self.workspace / "ledger.jsonl")

    def test_task_dispatch_audit_files_land_under_audit_directory(self) -> None:
        flow_ir = make_task_flow([sys.executable, str(self.task_script)])
        self.run_with_borrowed_ledger(flow_ir)

        self.assertEqual(self.audit_stdout_names(), ["T001-agent-1.stdout.log"])
        self.assertTrue((self.audit_directory() / "T001-agent-1.stderr.log").exists())
        self.assertEqual(list(self.workspace.glob("*.stdout.log")), [])
        self.assertEqual(list(self.workspace.glob("*.stderr.log")), [])
        audit_text = (self.audit_directory() / "T001-agent-1.stdout.log").read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn(TASK_AUDIT_MARKER, audit_text)

    def test_audit_tail_is_rebuildable_from_ledger_alone(self) -> None:
        """T020m 硬条件：仅凭账本重建路径，且 audit_tail 非空。"""
        flow_ir = make_task_flow([sys.executable, str(self.task_script)])
        ledger, _ = self.run_with_borrowed_ledger(flow_ir)
        ledger.close()  # 写侧收工，模拟进程结束

        fresh_view = self.fresh_readonly_ledger()
        relative_path = audit_relative_path_for_node(
            flow_ir.nodes["T001"], fresh_view.entries()
        )
        self.assertEqual(relative_path, "audit/T001-agent-1.stdout.log")
        self.assertIn(TASK_AUDIT_MARKER, _read_audit_tail(self.workspace, relative_path))

    def test_decision_dispatch_audit_lands_under_audit_directory(self) -> None:
        flow_ir = make_decision_flow(
            [sys.executable, str(self.task_script)],
            [sys.executable, str(self.judge_script)],
        )
        ledger, flow_state = self.run_with_borrowed_ledger(flow_ir)

        self.assertEqual(
            self.audit_stdout_names(),
            [
                "D001-judge-1.stdout.log",
                "T001-agent-1.stdout.log",
                "T002-agent-1.stdout.log",
            ],
        )
        self.assertEqual(list(self.workspace.glob("*.stdout.log")), [])

        ledger.close()
        fresh_view = self.fresh_readonly_ledger()
        relative_path = audit_relative_path_for_node(
            flow_ir.nodes["D001"], fresh_view.entries()
        )
        self.assertEqual(relative_path, "audit/D001-judge-1.stdout.log")
        self.assertIn(JUDGE_AUDIT_MARKER, _read_audit_tail(self.workspace, relative_path))

        completed_node_ids = {
            node_id
            for node_id in flow_ir.nodes
            if flow_state.node_status(node_id) == "done"
        }
        self.assertEqual(
            completed_node_ids, {"T001", "D001", "T002"},
            "判定节点判定通过后应到达 done，并继续推进到 T002",
        )

    def test_run_flow_borrows_ledger_without_closing_it(self) -> None:
        """T020d：借用语义——内核绝不 close 调用方持有的账本。"""
        flow_ir = make_task_flow([sys.executable, str(self.task_script)])
        ledger, _ = self.run_with_borrowed_ledger(flow_ir)

        self.assertGreater(ledger.last_seq, 0)
        self.assertTrue(ledger.verify_chain())
        # 白盒契约：借用期间内核不得 close（close 会把写句柄置 None）
        self.assertIsNotNone(
            ledger._write_handle, "run_flow 借用账本时不得 close 它"
        )
        appended_entry = ledger.append(
            "probe_after_borrow", {"probe": True}, "probe-after-borrow-1", flow_ir.run_name
        )
        self.assertEqual(appended_entry.seq, ledger.last_seq)
        self.assertTrue(ledger.verify_chain(), "借用后继续追加不得破坏哈希链")


class SameAttemptOverwriteTestCase(unittest.TestCase):
    """同名策略：同 attempt 重投覆盖同名文件，不产生第二个名字。"""

    def test_repeated_dispatch_with_same_attempt_overwrites_single_audit_file(self) -> None:
        adapter = FallbackAdapter()
        with tempfile.TemporaryDirectory(prefix="flowrun_audit_overwrite_") as temp_dir_name:
            workspace = Path(temp_dir_name)
            script_path = workspace / "fake_task_agent.py"
            packet_path = workspace / "packet.md"
            packet_path.write_text("实现登录 API。", encoding="utf-8")
            audit_artifact = AuditArtifact(
                workspace / "audit", "T001", AUDIT_KIND_AGENT, 1
            )

            write_reporting_script(script_path, FIRST_REPORT_MARKER)
            asyncio.run(
                adapter.dispatch(
                    packet_path, [sys.executable, str(script_path)], workspace, 30.0,
                    audit_artifact=audit_artifact,
                )
            )
            write_reporting_script(script_path, SECOND_REPORT_MARKER)
            asyncio.run(
                adapter.dispatch(
                    packet_path, [sys.executable, str(script_path)], workspace, 30.0,
                    audit_artifact=audit_artifact,
                )
            )

            self.assertEqual(
                sorted(path.name for path in (workspace / "audit").glob("*.stdout.log")),
                ["T001-agent-1.stdout.log"],
            )
            audit_text = (workspace / "audit" / "T001-agent-1.stdout.log").read_text(
                encoding="utf-8", errors="replace"
            )
            self.assertIn(SECOND_REPORT_MARKER, audit_text)
            self.assertNotIn(FIRST_REPORT_MARKER, audit_text)


class UnavailableAdapterAuditTestCase(unittest.TestCase):
    """防御分支：probe 通过却无适配器时，审计同样归位 audit/、同样命名。"""

    def test_no_adapter_audit_lands_under_audit_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="flowrun_audit_no_adapter_") as temp_dir_name:
            audit_directory = Path(temp_dir_name) / "audit"
            node = support.make_node("T009")

            dispatch_result = _unavailable_agent_result(
                node, "ghost-agent", audit_directory, 2
            )

            expected_stdout = audit_directory / "T009-no-adapter-2.stdout.log"
            expected_stderr = audit_directory / "T009-no-adapter-2.stderr.log"
            self.assertEqual(dispatch_result.stdout_path, expected_stdout)
            self.assertEqual(dispatch_result.stderr_path, expected_stderr)
            self.assertTrue(expected_stdout.exists())
            self.assertTrue(expected_stderr.exists())
            self.assertIn("ghost-agent", expected_stdout.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
