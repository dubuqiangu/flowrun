"""todo 投影测试共享支撑：路径引导、测试图构造器、todo 投影测试基类。

被 test_todo_projection*.py 复用（文件名不带 test_ 前缀，避免被 unittest 收集）。
"""

import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import kernel_test_support

import kernel_test_support as support  # noqa: E402  (负责把 src/ 加进 sys.path)

from flowrun.ir.schema import (  # noqa: E402
    NODE_KIND_DECISION,
    NODE_KIND_HUMAN,
    NODE_KIND_JOIN,
)
from flowrun.kernel.todo_projection import write_todo  # noqa: E402
from flowrun.state.ledger import EventLedger  # noqa: E402


def make_todo_test_flow():
    """todo 测试用图：任务 → 判定 → 人审 → join，覆盖全部节点形态。"""
    task_node = support.make_node("T001", label="实现登录API", outputs=["src/auth.py"])
    second_task = support.make_node("T002", label="实现登录UI")
    decision_node = support.make_node("D001", kind=NODE_KIND_DECISION, label="验收通过?")
    human_node = support.make_node("H001", kind=NODE_KIND_HUMAN, label="发布人审")
    join_node = support.make_node("J001", kind=NODE_KIND_JOIN, label="收口")
    edges = [
        support.make_edge("T001", "T002"),
        support.make_edge("T002", "D001"),
        support.make_edge("D001", "H001", when="ok"),
        support.make_edge("H001", "J001", when="all"),
    ]
    return support.make_flow_ir(
        [task_node, second_task, decision_node, human_node, join_node], edges
    )


def make_marker_label_flow():
    """label 文本里含旧式裸标记的图：label 是流程作者的自由文本，绝不触发指令。"""
    first_task = support.make_node("T001", label="check @pause gate")
    second_task = support.make_node("T002", label="skip @skip stage")
    third_task = support.make_node("T003", label="resume @resume now")
    edges = [support.make_edge("T001", "T002"), support.make_edge("T002", "T003")]
    return support.make_flow_ir([first_task, second_task, third_task], edges)


class TodoProjectionHarnessTestCase(unittest.TestCase):
    """todo 投影测试公共夹具：临时工作区 + 账本 + 测试图。"""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_todo_test_")
        self.workspace = Path(self._temp_dir.name) / "workspace"
        self.workspace.mkdir(parents=True)
        self.flow_ir = make_todo_test_flow()
        self.ledger = EventLedger(self.workspace / "ledger.jsonl")
        self.todo_path = self.workspace / "todo.md"

    def tearDown(self) -> None:
        self.ledger.close()
        self._temp_dir.cleanup()

    def _prime_ledger_and_write_todo(self) -> None:
        self.ledger.append(
            "flow_started",
            {"entry_nodes": ["H001"], "planned_nodes": ["T001", "T002"]},
            "run-flow-started", self.flow_ir.run_name,
        )
        # H001 必须先收到 human_requested 才是 awaiting_human——只有此时勾选才被接受
        # （未走到人审即勾选的拦截见 tests/test_todo_projection_human_guard.py）。
        self.ledger.append(
            "human_requested", {}, "run-H001-request-1", self.flow_ir.run_name, "H001"
        )
        self.ledger.append("node_done", {}, "run-T002-done-1", self.flow_ir.run_name, "T002")
        write_todo(self.flow_ir, self.ledger.replay(), self.todo_path)

    def _rewrite_todo_line(self, old_fragment: str, new_fragment: str) -> None:
        todo_text = self.todo_path.read_text(encoding="utf-8")
        self.assertIn(old_fragment, todo_text)
        self.todo_path.write_text(
            todo_text.replace(old_fragment, new_fragment), encoding="utf-8"
        )
