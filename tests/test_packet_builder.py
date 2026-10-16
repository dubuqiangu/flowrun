"""packet_builder 测试（域D1）：任务包/判定包内容断言。

全部 stdlib unittest，零配置直跑（python -m unittest discover -s tests）。
"""
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
from flowrun.kernel.judge import EvidencePack  # noqa: E402
from flowrun.kernel.packet_builder import (  # noqa: E402
    build_judge_packet,
    build_task_packet,
    edge_number,
)


def make_release_flow_ir() -> FlowIR:
    """样例发布流（design.md §4.1 缩编）：T001 -> D001 三路出边。"""
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
                outputs=["src/auth.py", "test/auth.spec.ts"],
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
                agent=None,
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
        outputs_present={"src/auth.py": True, "test/auth.spec.ts": False},
        audit_tail="全部测试通过\nDONE",
    )


class BuildTaskPacketTest(unittest.TestCase):
    """任务包：frontmatter + 任务描述 + 上游产物引用。"""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun-packet-test-")
        self.work_dir = Path(self._temp_dir.name)
        self.packet_dir = self.work_dir / "packets"
        self.flow_ir = make_release_flow_ir()

    def tearDown(self):
        self._temp_dir.cleanup()

    def test_packet_file_written_and_path_returned(self):
        node = self.flow_ir.nodes["T002"]
        packet_path = build_task_packet(self.flow_ir, node, self.work_dir, self.packet_dir)
        self.assertEqual(packet_path, self.packet_dir / "T002.packet.md")
        self.assertTrue(packet_path.exists())

    def test_frontmatter_contains_node_run_contract_budget(self):
        node = self.flow_ir.nodes["T001"]
        packet_text = build_task_packet(
            self.flow_ir, node, self.work_dir, self.packet_dir
        ).read_text(encoding="utf-8")
        self.assertIn("node: T001", packet_text)
        self.assertIn("run: release-login", packet_text)
        self.assertIn("contract: [src/auth.py, test/auth.spec.ts]", packet_text)
        self.assertIn("budget_tokens: 200000", packet_text)
        # frontmatter 以 --- 包裹
        self.assertTrue(packet_text.startswith("---\n"))
        self.assertIn("\n---\n", packet_text)

    def test_body_contains_label_and_self_report_request(self):
        node = self.flow_ir.nodes["T001"]
        packet_text = build_task_packet(
            self.flow_ir, node, self.work_dir, self.packet_dir
        ).read_text(encoding="utf-8")
        self.assertIn("实现登录API", packet_text)
        self.assertIn("完成后自报：", packet_text)

    def test_existing_upstream_outputs_listed(self):
        # T002 的直接前驱是 D001（无产物）——改用 T001 验证：给 D001 添一条
        # 来自带产物节点的入边场景不成立，这里直接给 D001 造上游产物节点。
        upstream_node = NodeSpec(
            node_id="U000",
            kind=NODE_KIND_TASK,
            label="准备阶段",
            agent=None,
            outputs=["docs/api-draft.md"],
            line_number=10,
        )
        self.flow_ir.nodes["U000"] = upstream_node
        self.flow_ir.edges.append(EdgeSpec("U000", "T001", None, 0, None, 11))
        (self.work_dir / "docs").mkdir()
        (self.work_dir / "docs" / "api-draft.md").write_text("草稿", encoding="utf-8")

        packet_text = build_task_packet(
            self.flow_ir, self.flow_ir.nodes["T001"], self.work_dir, self.packet_dir
        ).read_text(encoding="utf-8")
        self.assertIn("上游产物：docs/api-draft.md", packet_text)

    def test_missing_upstream_output_omitted(self):
        upstream_node = NodeSpec(
            node_id="U000",
            kind=NODE_KIND_TASK,
            label="准备阶段",
            agent=None,
            outputs=["docs/not-yet.md"],
            line_number=10,
        )
        self.flow_ir.nodes["U000"] = upstream_node
        self.flow_ir.edges.append(EdgeSpec("U000", "T001", None, 0, None, 11))
        # docs/not-yet.md 不存在 → 不列上游产物行
        packet_text = build_task_packet(
            self.flow_ir, self.flow_ir.nodes["T001"], self.work_dir, self.packet_dir
        ).read_text(encoding="utf-8")
        self.assertNotIn("上游产物：", packet_text)

    def test_empty_contract_renders_empty_list(self):
        node = self.flow_ir.nodes["T003"]
        packet_text = build_task_packet(
            self.flow_ir, node, self.work_dir, self.packet_dir
        ).read_text(encoding="utf-8")
        self.assertIn("contract: []", packet_text)


class BuildJudgePacketTest(unittest.TestCase):
    """判定包：候选边编号 + when 原文 + 证据包 + 严格 JSON 输出格式要求。"""

    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun-judge-packet-test-")
        self.work_dir = Path(self._temp_dir.name)
        self.packet_dir = self.work_dir / "packets"
        self.flow_ir = make_release_flow_ir()
        self.decision_node = self.flow_ir.nodes["D001"]
        self.candidate_edges = [
            edge
            for edge in self.flow_ir.edges
            if edge.source_id == "D001" and edge.when is not None
        ]

    def tearDown(self):
        self._temp_dir.cleanup()

    def test_packet_file_written_and_path_returned(self):
        packet_path = build_judge_packet(
            self.flow_ir,
            self.decision_node,
            self.candidate_edges,
            make_evidence_pack(),
            self.packet_dir,
        )
        self.assertEqual(packet_path, self.packet_dir / "D001.judge.md")
        self.assertTrue(packet_path.exists())

    def test_candidate_edges_listed_with_number_and_when_text(self):
        packet_text = build_judge_packet(
            self.flow_ir,
            self.decision_node,
            self.candidate_edges,
            make_evidence_pack(),
            self.packet_dir,
        ).read_text(encoding="utf-8")
        self.assertIn("1. D001->H001  when: ok", packet_text)
        self.assertIn("2. D001->T002  when: fail  retry: 2", packet_text)

    def test_evidence_sections_rendered(self):
        packet_text = build_judge_packet(
            self.flow_ir,
            self.decision_node,
            self.candidate_edges,
            make_evidence_pack(),
            self.packet_dir,
        ).read_text(encoding="utf-8")
        self.assertIn("exit_code=0", packet_text)
        self.assertIn("timed_out=False", packet_text)
        self.assertIn("semantic_done=False", packet_text)
        self.assertIn("duration_seconds=1.5", packet_text)
        self.assertIn("T001 src/auth.py: 存在", packet_text)
        self.assertIn("T001 test/auth.spec.ts: 缺失", packet_text)
        self.assertIn("全部测试通过", packet_text)

    def test_strict_json_output_requirement_present(self):
        packet_text = build_judge_packet(
            self.flow_ir,
            self.decision_node,
            self.candidate_edges,
            make_evidence_pack(),
            self.packet_dir,
        ).read_text(encoding="utf-8")
        self.assertIn("只输出一个严格 JSON 对象", packet_text)
        self.assertIn('{"edge":"<候选边编号>","confidence":<0-1之间的数字>', packet_text)
        self.assertIn('"evidence":["证据1","证据2"]}', packet_text)

    def test_frontmatter_marks_decision_judge_kind(self):
        packet_text = build_judge_packet(
            self.flow_ir,
            self.decision_node,
            self.candidate_edges,
            make_evidence_pack(),
            self.packet_dir,
        ).read_text(encoding="utf-8")
        self.assertIn("node: D001", packet_text)
        self.assertIn("run: release-login", packet_text)
        self.assertIn("kind: decision-judge", packet_text)
        self.assertIn("budget_tokens: 200000", packet_text)


class EdgeNumberTest(unittest.TestCase):
    def test_edge_number_is_source_arrow_target(self):
        edge = EdgeSpec("D001", "H001", "ok", 0, None, 7)
        self.assertEqual(edge_number(edge), "D001->H001")


if __name__ == "__main__":
    unittest.main()
