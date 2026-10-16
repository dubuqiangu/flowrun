"""T012 AGENTS.stub.md 生成器测试：桩内容五要素（packet 协议 / 自报 / 契约
核对 / 判定 JSON / 相对路径）+ 本流程契约一览逐段断言。

直跑：python -m unittest discover -s tests
"""

import tempfile
import unittest
from pathlib import Path

import sys

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))

import kernel_test_support as support  # noqa: E402

from flowrun.kernel.stub_generator import (  # noqa: E402
    AGENTS_STUB_FILENAME,
    generate_agents_stub,
)


class AgentsStubGeneratorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_stub_test_")
        self.work_dir = Path(self._temp_dir.name) / "workspace"

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def make_flow_ir_with_contracts(self) -> object:
        task_node_auth = support.make_node(
            "T001", label="实现登录API", outputs=["src/auth.py", "test/auth.spec.ts"]
        )
        task_node_ui = support.make_node(
            "T002", label="实现登录UI", outputs=["src/ui/Login.tsx"]
        )
        human_node = support.make_node("H001", kind="human", label="发布人审")
        return support.make_flow_ir([task_node_auth, task_node_ui, human_node], [])

    def generate_stub_text(self) -> str:
        stub_path = generate_agents_stub(self.make_flow_ir_with_contracts(), self.work_dir)
        self.assertEqual(stub_path, self.work_dir / AGENTS_STUB_FILENAME)
        return stub_path.read_text(encoding="utf-8")

    # ---------------------------------------------------------------- 五要素

    def test_stub_file_is_created_in_workspace(self) -> None:
        """桩落在工作区根的 AGENTS.stub.md，非空且可读。"""
        stub_text = self.generate_stub_text()
        self.assertTrue(stub_text.strip())

    def test_stub_documents_packet_protocol(self) -> None:
        """packet 协议段：packets/ 目录、frontmatter 四个机器字段。"""
        stub_text = self.generate_stub_text()
        for expected_fragment in (
            "packets/",
            ".packet.md",
            ".judge.md",
            "node",
            "run",
            "contract",
            "budget_tokens",
        ):
            self.assertIn(expected_fragment, stub_text)

    def test_stub_requires_self_report_in_final_reply(self) -> None:
        """自报段：完成后必须自报改了哪些文件、结果如何。"""
        stub_text = self.generate_stub_text()
        for expected_fragment in ("自报", "改了哪些文件", "结果"):
            self.assertIn(expected_fragment, stub_text)

    def test_stub_states_missing_contract_means_fail(self) -> None:
        """契约核对规则：缺件 = fail。"""
        stub_text = self.generate_stub_text()
        self.assertIn("缺任何一件", stub_text)
        self.assertIn("fail", stub_text)

    def test_stub_states_judge_strict_json_format(self) -> None:
        """判定输出格式：严格 JSON 的 edge/confidence/evidence 模板。"""
        stub_text = self.generate_stub_text()
        self.assertIn("严格 JSON", stub_text)
        for expected_fragment in ('"edge"', '"confidence"', '"evidence"', "D001->H001"):
            self.assertIn(expected_fragment, stub_text)

    def test_stub_states_workspace_relative_path_rule(self) -> None:
        """路径规则：一切产物路径相对工作区根。"""
        stub_text = self.generate_stub_text()
        self.assertIn("相对", stub_text)
        self.assertIn("工作区根", stub_text)

    # ---------------------------------------------------------------- 流程特定内容

    def test_stub_lists_this_flow_contracts(self) -> None:
        """契约一览列出本流程声明的全部 out= 产物。"""
        stub_text = self.generate_stub_text()
        self.assertIn("T001", stub_text)
        self.assertIn("src/auth.py", stub_text)
        self.assertIn("test/auth.spec.ts", stub_text)
        self.assertIn("T002", stub_text)
        self.assertIn("src/ui/Login.tsx", stub_text)

    def test_stub_mentions_run_name(self) -> None:
        """桩标明流程名，agent 可对照 packet 的 run 字段。"""
        stub_text = self.generate_stub_text()
        self.assertIn(support.DEFAULT_RUN_NAME, stub_text)

    def test_stub_notes_no_contract_nodes_are_simply_omitted(self) -> None:
        """无产物契约的节点不出现在契约一览里（人审节点不列）。"""
        stub_text = self.generate_stub_text()
        contract_list_section = stub_text.split("产物契约一览", 1)[1]
        self.assertNotIn("H001", contract_list_section)


if __name__ == "__main__":
    unittest.main()
