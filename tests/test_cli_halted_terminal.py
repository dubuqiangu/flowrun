"""CLI halted 终止态契约：run / resume / status 对既有 ``flow_halted`` 的上报口径。

直跑：python -m unittest discover -s tests

背景（第七轮定稿）：``flow_halted`` 目前只有投影层支持（``state/flow_state.py``），
M1 尚无其生产者（生产路径随 T019 落地）。本轮**不新增 halt 子命令/特性**，而是把
``EXIT_FLOW_HALTED = 2`` 从"死常量"钉成**可测契约**——账本里一旦已存在 ``flow_halted``：

- ``resume`` 必须**拒绝恢复**并退 **2**，且不得落任何 ``flow_resumed``
  （halted 是终止态，不被"人审豁免"式 resume 覆盖）；
- ``status`` 只读报出 ``flow: halted`` 并退 **2**（使终止异常态可被脚本/CI 检出）；
- ``run`` 收口时 ``is_halted`` → 退 **2**（不重复驱动已终止的流）。

``flow_halted`` 无生产者，故夹具直接把它写进账本（不走 CLI，也不新增命令）。
"""

import sys
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import cli_harness_support

from cli_harness_support import CliHarnessTestCase  # noqa: E402
from flowrun.cli import EXIT_FLOW_HALTED  # noqa: E402
from flowrun.state.ledger import EventLedger  # noqa: E402

LINEAR_RUN_NAME = "cli-linear"
LINEAR_NODE_IDS = ["T001", "T002"]


class CliHaltedTerminalTestCase(CliHarnessTestCase):
    """halted 终止态的三通道退出码契约（2=halted，见 cli.py 退出码表）。"""

    def seed_halted_workspace(self, flow_path: Path) -> None:
        """写好"已 halted"的工作区：flow_started（与图身份一致）+ flow_halted。"""
        self.write_linear_flow(flow_path)
        self.write_workspace_flow_started_ledger(LINEAR_RUN_NAME, LINEAR_NODE_IDS)
        ledger = EventLedger(self.workspace / "ledger.jsonl")
        try:
            ledger.append(
                "flow_halted",
                {"reason": "seed_for_contract_test"},
                f"{LINEAR_RUN_NAME}-flow-halted",
                LINEAR_RUN_NAME,
            )
        finally:
            ledger.close()

    def read_ledger_event_types(self) -> list[str]:
        ledger = EventLedger.open_readonly(self.workspace / "ledger.jsonl")
        try:
            return [entry.type for entry in ledger.entries()]
        finally:
            ledger.close()

    def test_exit_flow_halted_constant_is_two(self) -> None:
        """钉住常量本身：退出码表 2=halted，是脚本可依赖的契约。"""
        self.assertEqual(EXIT_FLOW_HALTED, 2)

    def test_resume_refuses_halted_flow_and_exits_two(self) -> None:
        """halted 是终止态：resume 拒绝恢复、退 2，且不得落 flow_resumed 覆盖它。"""
        flow_path = self.test_root / "halted.mmd"
        self.seed_halted_workspace(flow_path)

        exit_code, stdout_text, stderr_text = self.invoke_cli(
            ["resume", "--flow", str(flow_path), "--workspace", str(self.workspace)]
        )

        self.assertEqual(exit_code, EXIT_FLOW_HALTED)
        self.assertIn("halted", stderr_text)
        self.assertIn("拒绝恢复", stderr_text)
        self.assertEqual(stdout_text, "", "拒绝路径不得打印成功恢复文案")
        self.assertNotIn(
            "flow_resumed",
            self.read_ledger_event_types(),
            "halted 不被 resume 覆盖——终止态一旦成立就不再接受人审豁免",
        )

    def test_status_reports_halted_flow_and_exits_two(self) -> None:
        """status 是只读检出：报 flow: halted 并把终止态以退出码 2 暴露给脚本。"""
        flow_path = self.test_root / "halted.mmd"
        self.seed_halted_workspace(flow_path)

        exit_code, stdout_text, _stderr_text = self.invoke_cli(
            ["status", "--flow", str(flow_path), "--workspace", str(self.workspace)]
        )

        self.assertEqual(exit_code, EXIT_FLOW_HALTED)
        self.assertIn("flow: halted", stdout_text)

    def test_run_on_halted_ledger_exits_two(self) -> None:
        """run 通道：账本已 halted 时收口即退 2，不把它当暂停/等待重跑。"""
        flow_path = self.test_root / "halted.mmd"
        self.seed_halted_workspace(flow_path)

        exit_code, stdout_text, _stderr_text = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(self.workspace)]
        )

        self.assertEqual(exit_code, EXIT_FLOW_HALTED)
        self.assertIn("flow: halted", stdout_text)


if __name__ == "__main__":
    unittest.main()
