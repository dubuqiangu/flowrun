"""kernel judge 测试共享支撑：假判定 agent 脚本路径、样例判定流、证据包、
判定测试基类（每个用例独立的 work_dir / packet_dir / ledger / 计数适配器）。

被 test_kernel_judge*.py 复用（文件名不带 test_ 前缀，避免被 unittest 收集）。
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
from flowrun.kernel.judge import EvidencePack, judge_decision  # noqa: E402
from flowrun.state.ledger import EventLedger  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
FAKE_JUDGE_AGENT_SCRIPT = FIXTURES_DIR / "fake_judge_agent.py"


class CountingFallbackAdapter(FallbackAdapter):
    """记录投递次数的兜底适配器（判定走真实子进程 + 审计落盘路径）。"""

    def __init__(self):
        super().__init__()
        self.dispatch_count = 0
        self.dispatched_packet_paths: list[Path] = []

    async def dispatch(self, packet_path, agent_command, work_dir, timeout_seconds,
                       *, audit_artifact=None):
        self.dispatch_count += 1
        self.dispatched_packet_paths.append(Path(packet_path))
        return await super().dispatch(
            packet_path, agent_command, work_dir, timeout_seconds,
            audit_artifact=audit_artifact,
        )


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
