"""域D2 · run_flow 账本句柄生命周期测试。

主证据：run_flow 返回/抛错后账本文件能被 ``os.replace`` 改名——Windows 上
文件仍被占用时 rename 会失败，因此改名成功即证明追加句柄已释放。
辅证：异常路径同样不得留下未关闭句柄。

直跑：python -m unittest discover -s tests
"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

import sys

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))

import kernel_test_support as support  # noqa: E402

from flowrun.kernel.engine import (  # noqa: E402
    EngineHooks,
    FlowWorkspaceMismatchError,
    run_flow,
)


class LedgerLifecycleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_ledger_lifecycle_")
        self.workspace = Path(self._temp_dir.name) / "workspace"
        self.stubs = support.KernelHookStubs()

    def run_dir(self, run_name: str = support.DEFAULT_RUN_NAME) -> Path:
        """本次测试 run 的实际目录（T022：namespaced 布局在 <ws>/<run>/）。"""
        return support.run_dir_for(self.workspace, run_name)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def ledger_path(self, run_name: str = support.DEFAULT_RUN_NAME) -> Path:
        return self.run_dir(run_name) / "ledger.jsonl"

    def assert_ledger_handle_released(
        self, run_name: str = support.DEFAULT_RUN_NAME
    ) -> Path:
        """账本文件可被改名 = 追加句柄已关闭（Windows 上被占用时 rename 失败）。"""
        original_path = self.ledger_path(run_name)
        renamed_path = original_path.with_suffix(".moved")
        os.replace(original_path, renamed_path)
        self.assertTrue(renamed_path.exists())
        self.assertFalse(original_path.exists())
        return renamed_path

    def test_completed_flow_releases_ledger_handle(self) -> None:
        flow_ir = support.make_flow_ir([support.make_node("T001")], [])
        final_state = asyncio.run(
            run_flow(flow_ir, self.workspace, self.stubs.build_hooks())
        )
        self.assertTrue(final_state.is_done())
        self.assert_ledger_handle_released()

    def test_workspace_mismatch_releases_ledger_handle(self) -> None:
        """同 run 名但图被替换（节点超集）→ 身份校验拒绝后，run 自建账本句柄已释放。

        T022 说明：namespaced 下"同工作区不同 run"各有目录、不再是身份冲突；
        仍会拒绝的是**同一份账本**承载不同计划节点集（K2 超集）这一种 mismatch，
        它发生在 run 自建自管账本的路径上，故句柄释放断言继续有效。
        """
        first_flow_ir = support.make_flow_ir([support.make_node("T001")], [])
        asyncio.run(run_flow(first_flow_ir, self.workspace, self.stubs.build_hooks()))

        second_flow_ir = support.make_flow_ir(
            [support.make_node("T001"), support.make_node("T002")],
            [support.make_edge("T001", "T002")],
        )
        with self.assertRaises(FlowWorkspaceMismatchError):
            asyncio.run(
                run_flow(second_flow_ir, self.workspace, self.stubs.build_hooks())
            )
        self.assert_ledger_handle_released()

    def test_snapshot_hook_error_releases_ledger_handle(self) -> None:
        def raising_snapshot(ledger, flow_state) -> None:
            raise RuntimeError("snapshot hook 故意抛错（模拟主循环内未捕获异常）")

        raising_hooks = EngineHooks(
            build_task_packet=self.stubs.build_task_packet,
            probe_agent=self.stubs.probe_agent,
            dispatch_task=self.stubs.dispatch_task,
            judge_decision=self.stubs.judge_decision,
            snapshot=raising_snapshot,
        )
        flow_ir = support.make_flow_ir([support.make_node("T001")], [])
        with self.assertRaises(RuntimeError):
            asyncio.run(run_flow(flow_ir, self.workspace, raising_hooks))
        self.assert_ledger_handle_released()


if __name__ == "__main__":
    unittest.main()
