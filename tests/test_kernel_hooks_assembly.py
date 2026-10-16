"""域D2 · 生产装配桥接测试（hooks_assembly）：多上游证据合并后的总量上限（FIX-B5）。

单路上游的 audit_tail 已在读取时截到 2000 字符；多路合并后可达 k×2000，
必须再做一次总量截断（保尾部 + 省略标记）。

直跑：python -m unittest discover -s tests
"""

import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))

import kernel_test_support as support  # noqa: E402
from flowrun.ir.schema import NODE_KIND_DECISION  # noqa: E402
from flowrun.kernel.engine_evidence import (  # noqa: E402
    EvidencePack,
    UpstreamNodeEvidence,
)
from flowrun.kernel.hooks_assembly import (  # noqa: E402
    _AUDIT_TAIL_MAX_CHARS,
    _MERGED_AUDIT_TRUNCATION_MARKER,
    build_judge_evidence_pack,
)

UPSTREAM_TAIL_CHARS = 1500  # 单路上游 < 2000（不被单路截断），但两路合并后超限


def make_audit_text(node_id: str) -> str:
    """构造恰好 UPSTREAM_TAIL_CHARS 字符的上游 stdout 审计文本（尾部带标识）。"""
    tail_marker = f" <{node_id} 尾部结束>"
    padding_length = UPSTREAM_TAIL_CHARS - len(tail_marker)
    return f"{node_id} 审计正文 ".ljust(padding_length, "·") + tail_marker


class HooksAssemblyEvidenceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_hooks_assembly_test_")
        self.work_dir = Path(self._temp_dir.name)
        first_upstream = support.make_node("T001")
        second_upstream = support.make_node("T002")
        self.decision_node = support.make_node("D001", kind=NODE_KIND_DECISION)
        self.flow_ir = support.make_flow_ir(
            [first_upstream, second_upstream, self.decision_node],
            [
                support.make_edge("T001", "D001"),
                support.make_edge("T002", "D001"),
            ],
        )
        self.audit_text_by_node = {
            "T001": make_audit_text("T001"),
            "T002": make_audit_text("T002"),
        }
        for node_id, audit_text in self.audit_text_by_node.items():
            (self.work_dir / f"{node_id}-stdout.log").write_text(audit_text, encoding="utf-8")

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def build_engine_evidence(self, node_ids: list[str]) -> EvidencePack:
        return EvidencePack(
            decision_node_id="D001",
            upstream_evidence=[
                UpstreamNodeEvidence(
                    node_id=node_id,
                    exit_code=0,
                    timed_out=False,
                    semantic_done=True,
                    expected_outputs=[],
                    present_outputs=[],
                    stdout_file_name=f"{node_id}-stdout.log",
                )
                for node_id in node_ids
            ],
        )

    # ---------------------------------------------------------------- 合并总量上限

    def test_two_upstream_tails_are_truncated_to_single_budget(self) -> None:
        """FIX-B5 复现：两路上游各 1500 字符 → 合并后不得超过 2000 字符总量上限。"""
        packed_evidence = build_judge_evidence_pack(
            self.flow_ir, self.build_engine_evidence(["T001", "T002"]), self.work_dir, []
        )

        self.assertLessEqual(
            len(packed_evidence.audit_tail),
            _AUDIT_TAIL_MAX_CHARS,
            f"合并后总量必须 ≤ {_AUDIT_TAIL_MAX_CHARS} 字符（含省略标记）",
        )
        self.assertTrue(
            packed_evidence.audit_tail.startswith(_MERGED_AUDIT_TRUNCATION_MARKER),
            "截断必须带省略标记",
        )
        self.assertTrue(
            packed_evidence.audit_tail.endswith(self.audit_text_by_node["T002"]),
            "截断必须保尾部（最近的上游证据可读）",
        )

    def test_single_upstream_tail_is_not_marked(self) -> None:
        """单路上游未超限：不得加省略标记（避免无谓噪声）。"""
        packed_evidence = build_judge_evidence_pack(
            self.flow_ir, self.build_engine_evidence(["T001"]), self.work_dir, []
        )

        self.assertEqual(packed_evidence.audit_tail, self.audit_text_by_node["T001"])
        self.assertNotIn(_MERGED_AUDIT_TRUNCATION_MARKER, packed_evidence.audit_tail)

    def test_short_multi_upstream_merge_is_not_marked(self) -> None:
        """多路合并但总量未超限：两路证据都应完整保留且无标记。"""
        short_text_by_node = {"T001": "T001 短证据", "T002": "T002 短证据"}
        for node_id, audit_text in short_text_by_node.items():
            (self.work_dir / f"{node_id}-stdout.log").write_text(audit_text, encoding="utf-8")

        packed_evidence = build_judge_evidence_pack(
            self.flow_ir, self.build_engine_evidence(["T001", "T002"]), self.work_dir, []
        )

        self.assertNotIn(_MERGED_AUDIT_TRUNCATION_MARKER, packed_evidence.audit_tail)
        for audit_text in short_text_by_node.values():
            self.assertIn(audit_text, packed_evidence.audit_tail)


if __name__ == "__main__":
    unittest.main()
