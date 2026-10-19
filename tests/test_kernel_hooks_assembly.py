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
from flowrun.adapters.base import (  # noqa: E402
    AUDIT_KIND_AGENT,
    audit_stdout_relative_path,
)
from flowrun.ir.schema import NODE_KIND_DECISION, NODE_KIND_JOIN  # noqa: E402
from flowrun.kernel.engine_evidence import (  # noqa: E402
    EvidencePack,
    UpstreamNodeEvidence,
)
from flowrun.kernel.hooks_assembly import (  # noqa: E402
    _AUDIT_TAIL_MAX_CHARS,
    _MERGED_AUDIT_TRUNCATION_MARKER,
    build_judge_evidence_pack,
)
from flowrun.state.ledger import EventLedger  # noqa: E402

UPSTREAM_TAIL_CHARS = 1500  # 单路上游 < 2000（不被单路截断），但两路合并后超限
UPSTREAM_ATTEMPT = 1  # 上游任务的第一次投递（与账本 attempt 口径一致）


def audit_relative_path_for(node_id: str) -> str:
    """上游审计 stdout 相对工作区根的路径（写侧/读侧共用的命名契约）。"""
    return audit_stdout_relative_path(node_id, AUDIT_KIND_AGENT, UPSTREAM_ATTEMPT).as_posix()


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
            audit_path = self.work_dir / audit_relative_path_for(node_id)
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            audit_path.write_text(audit_text, encoding="utf-8")

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
                    audit_relative_path=audit_relative_path_for(node_id),
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
            (self.work_dir / audit_relative_path_for(node_id)).write_text(
                audit_text, encoding="utf-8"
            )

        packed_evidence = build_judge_evidence_pack(
            self.flow_ir, self.build_engine_evidence(["T001", "T002"]), self.work_dir, []
        )

        self.assertNotIn(_MERGED_AUDIT_TRUNCATION_MARKER, packed_evidence.audit_tail)
        for audit_text in short_text_by_node.values():
            self.assertIn(audit_text, packed_evidence.audit_tail)


class HooksAssemblyContractGapTestCase(unittest.TestCase):
    """产物契约缺口必须进判定证据（T020o 的 evidence 面直测）。

    引擎侧"契约缺件 → ``node_failed(reason=missing_outputs)``"由引擎测试钉；
    本文件钉判定侧**看得见缺件**：证据包要逐项给出 ``outputs_present`` 的真假，
    多上游合并时产物键带节点前缀。否则同路径会互相覆盖，判定看到的是假的"齐"，
    缺件节点会被误判为完成。

    第三项覆盖 join 上游展开路径：join 自身无投递事件，证据要上溯到它的前驱
    任务，并**按工作区现状**逐项核对前驱声明的产物（缺件记 False）。
    """

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_contract_gap_")
        self.addCleanup(self._temp_dir.cleanup)
        self.work_dir = Path(self._temp_dir.name)
        self.decision_node = support.make_node("D001", kind=NODE_KIND_DECISION)

    def upstream_evidence(
        self, node_id: str, expected_outputs: list[str], present_outputs: list[str]
    ) -> UpstreamNodeEvidence:
        return UpstreamNodeEvidence(
            node_id=node_id,
            exit_code=0,
            timed_out=False,
            semantic_done=True,
            expected_outputs=list(expected_outputs),
            present_outputs=list(present_outputs),
            audit_relative_path=None,
        )

    def fork_flow_ir(self) -> object:
        first_upstream = support.make_node(
            "T001", outputs=["src/auth.py", "test/auth.spec.ts"]
        )
        second_upstream = support.make_node("T002", outputs=["src/auth.py"])
        return support.make_flow_ir(
            [first_upstream, second_upstream, self.decision_node],
            [
                support.make_edge("T001", "D001"),
                support.make_edge("T002", "D001"),
            ],
        )

    def test_missing_contract_item_is_reported_as_false(self) -> None:
        """声明了两件、工作区只有一件：证据必须给出 True/False 明细。"""
        packed_evidence = build_judge_evidence_pack(
            self.fork_flow_ir(),
            EvidencePack(
                decision_node_id="D001",
                upstream_evidence=[
                    self.upstream_evidence(
                        "T001", ["src/auth.py", "test/auth.spec.ts"], ["src/auth.py"]
                    )
                ],
            ),
            self.work_dir,
            [],
        )

        self.assertEqual(
            packed_evidence.outputs_present,
            {"src/auth.py": True, "test/auth.spec.ts": False},
        )
        self.assertEqual(packed_evidence.exit_code, 0)

    def test_merged_outputs_are_prefixed_per_upstream_node(self) -> None:
        """同路径产物分属两个上游：合并后键带节点前缀，缺件项仍是 False。"""
        packed_evidence = build_judge_evidence_pack(
            self.fork_flow_ir(),
            EvidencePack(
                decision_node_id="D001",
                upstream_evidence=[
                    self.upstream_evidence("T001", ["src/auth.py"], ["src/auth.py"]),
                    self.upstream_evidence("T002", ["src/auth.py"], []),
                ],
            ),
            self.work_dir,
            [],
        )

        self.assertEqual(
            packed_evidence.outputs_present,
            {"T001:src/auth.py": True, "T002:src/auth.py": False},
        )

    def test_join_upstream_expands_to_predecessors_with_workspace_contract_check(self) -> None:
        """join 上游展开为前驱任务；产物核对按工作区现状，缺件记 False。"""
        first_predecessor = support.make_node(
            "T001", outputs=["src/auth.py", "test/auth.spec.ts"]
        )
        join_node = support.make_node("J001", kind=NODE_KIND_JOIN)
        flow_ir = support.make_flow_ir(
            [first_predecessor, join_node, self.decision_node],
            [
                support.make_edge("T001", "J001"),
                support.make_edge("J001", "D001"),
            ],
        )
        (self.work_dir / "src").mkdir(parents=True, exist_ok=True)
        (self.work_dir / "src" / "auth.py").write_text("print('ok')\n", encoding="utf-8")
        ledger = EventLedger(self.work_dir / "ledger.jsonl")
        self.addCleanup(ledger.close)
        ledger.append(
            "agent_exited",
            {"exit_code": 0, "timed_out": False, "semantic_done": True},
            f"{flow_ir.run_name}-T001-agent-exited-1",
            flow_ir.run_name,
            "T001",
        )

        packed_evidence = build_judge_evidence_pack(
            flow_ir,
            EvidencePack(
                decision_node_id="D001",
                upstream_evidence=[
                    UpstreamNodeEvidence(
                        node_id="J001",
                        exit_code=None,
                        timed_out=False,
                        semantic_done=False,
                        expected_outputs=[],
                        present_outputs=[],
                        audit_relative_path=None,
                    )
                ],
            ),
            self.work_dir,
            ledger.entries(),
        )

        self.assertEqual(packed_evidence.node_id, "T001", "join 自身无投递事件，证据上溯到前驱")
        self.assertEqual(packed_evidence.exit_code, 0, "退出码取账本最近一次 agent_exited")
        self.assertTrue(packed_evidence.semantic_done)
        self.assertEqual(
            packed_evidence.outputs_present,
            {"src/auth.py": True, "test/auth.spec.ts": False},
            "工作区里缺的那一件必须记 False",
        )


if __name__ == "__main__":
    unittest.main()
