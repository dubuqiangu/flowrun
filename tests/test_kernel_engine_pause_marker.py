"""T020o 缺口① · 引擎级 ``<!-- @pause -->`` 用例。

既有覆盖只有两层，都不是引擎级：CLI 级（tests/test_cli_resume.py，端到端跑 CLI 再
读状态输出）与投影级（tests/test_todo_projection.py，只断言 todo 头部横幅）。缺的是
中间那一层：``<!-- @pause -->`` 落成 ``user_directive{"directive": "pause"}`` 之后，
``run_flow`` 是否真的在等待点停下（不投递、不推进边、复跑不刷账），以及解除暂停后
复跑能否续推到 ``flow_done``。本文件钉这条引擎级路径。

结构性依赖：暂停需要"踢得动的目标"——链式任务一轮就会跑完，故用任务 → 人审 → 任务
的图造出等待点，在等待点把尾部任务（planned，属可暂停态）按用户操作挂起。

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

from flowrun.ir.schema import NODE_KIND_HUMAN  # noqa: E402
from flowrun.kernel.engine import run_flow  # noqa: E402
from flowrun.kernel.todo_projection import apply_user_marks  # noqa: E402
from flowrun.state.ledger import EventLedger  # noqa: E402

PAUSE_MARKER_TEXT = "<!-- @pause -->"
RESUME_MARKER_TEXT = "<!-- @resume -->"


class EnginePauseMarkerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_engine_pause_")
        # 注册为 cleanup（LIFO）：测试期间注册的账本 close 先执行，之后才删临时目录；
        # 若在 tearDown 里删目录，Windows 上仍打开的 ledger.jsonl 会让 rmtree 失败。
        self.addCleanup(self._temp_dir.cleanup)
        self.workspace = Path(self._temp_dir.name) / "workspace"
        self.stubs = support.KernelHookStubs()
        self.todo_path = self.run_dir() / "todo.md"

    def run_dir(self) -> Path:
        """本次测试 run 的实际目录（T022：namespaced 布局在 <ws>/<run>/）。"""
        return support.run_dir_for(self.workspace, support.DEFAULT_RUN_NAME)

    def run_flow_once(self, flow_ir):
        return asyncio.run(run_flow(flow_ir, self.workspace, self.stubs.build_hooks()))

    def reopened_ledger(self) -> EventLedger:
        """重开账本读取落账结果；注册清理，避免测试结束后遗留写句柄（ResourceWarning）。"""
        ledger = EventLedger(self.run_dir() / "ledger.jsonl")
        self.addCleanup(ledger.close)
        return ledger

    def apply_todo_marks(self, flow_ir) -> int:
        """走 CLI 同款通道：打开账本 → 应用 todo 标记 → 关账本 → 由 run_flow 重放。"""
        mark_ledger = self.reopened_ledger()
        try:
            return apply_user_marks(
                flow_ir, self.todo_path, mark_ledger, mark_ledger.replay()
            )
        finally:
            mark_ledger.close()

    def rewrite_todo(self, old_text: str, new_text: str) -> None:
        todo_text = self.todo_path.read_text(encoding="utf-8")
        self.assertIn(old_text, todo_text, "待改写的 todo 片段必须存在（投影格式变了？）")
        self.todo_path.write_text(
            todo_text.replace(old_text, new_text, 1), encoding="utf-8"
        )

    def make_flow_ir(self):
        task_node = support.make_node("T001", label="实现登录API")
        human_node = support.make_node("H001", kind=NODE_KIND_HUMAN, label="发布人审")
        release_task = support.make_node("T002", label="收尾")
        return support.make_flow_ir(
            [task_node, human_node, release_task],
            [support.make_edge("T001", "H001"), support.make_edge("H001", "T002")],
        )

    def test_pause_marker_suspends_engine_then_resume_continues(self) -> None:
        flow_ir = self.make_flow_ir()

        # 第一轮：跑到人审等待点（T002 仍是 planned，正是可被 @pause 挂起的目标）
        waiting_state = self.run_flow_once(flow_ir)
        self.assertEqual(waiting_state.node_status("H001"), "awaiting_human")
        self.assertEqual(waiting_state.node_status("T002"), "planned")
        self.assertFalse(waiting_state.is_paused())
        self.assertEqual([call[0] for call in self.stubs.dispatch_calls], ["T001"])

        # 用户在 T002 行尾加 <!-- @pause -->（CLI 同款通道落账）
        self.rewrite_todo("- [ ] T002 收尾", f"- [ ] T002 收尾  {PAUSE_MARKER_TEXT}")
        self.assertEqual(self.apply_todo_marks(flow_ir), 1, "@pause 恰落一条 user_directive")

        # 引擎级：run_flow 在暂停等待点停下——不投递、不推进边
        paused_state = self.run_flow_once(flow_ir)
        self.assertTrue(paused_state.is_paused(), "user_directive{pause} 后引擎必须挂起")
        self.assertEqual(paused_state.node_status("T002"), "paused")
        self.assertFalse(paused_state.is_done())
        self.assertEqual(
            [call[0] for call in self.stubs.dispatch_calls], ["T001"],
            "暂停轮不得投递任何新节点",
        )
        paused_ledger = self.reopened_ledger()
        self.assertEqual(len(support.events_of_type(paused_ledger, "agent_dispatched")), 1)
        events_before_rerun = len(paused_ledger.entries())

        # 复跑（尚未解除暂停）：等待点先于一切推进，账本零新增（FIX-B2）
        rerun_state = self.run_flow_once(flow_ir)
        self.assertTrue(rerun_state.is_paused())
        self.assertEqual(
            len(self.reopened_ledger().entries()), events_before_rerun,
            "已暂停的流再 run 不得产生任何新事件",
        )
        self.assertEqual([call[0] for call in self.stubs.dispatch_calls], ["T001"])

        # 人审放行 + 行尾 @resume：暂停解除后复跑续推到 done
        self.rewrite_todo(
            f"- [ ] T002 收尾  <!-- paused -->",
            f"- [ ] T002 收尾  <!-- paused --> {RESUME_MARKER_TEXT}",
        )
        self.rewrite_todo("- [ ] H001 发布人审", "- [x] H001 发布人审")
        self.assertEqual(
            self.apply_todo_marks(flow_ir), 2,
            "人审勾选 + @resume 各落一条事件",
        )

        final_state = self.run_flow_once(flow_ir)
        self.assertTrue(final_state.is_done())
        self.assertFalse(final_state.is_paused())
        self.assertEqual(final_state.node_status("T002"), "done")
        self.assertEqual(
            [call[0] for call in self.stubs.dispatch_calls], ["T001", "T002"],
            "解除暂停后只补投暂停点之后的节点",
        )


if __name__ == "__main__":
    unittest.main()
