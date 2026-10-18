"""CLI 身份校验先于写入测试：run 退 4 时工作区必须零痕迹（账本 / stub / 内核目录）。

直跑：python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import cli_harness_support

from cli_harness_support import (  # noqa: E402  (负责把 src/ 加进 sys.path)
    CliHarnessTestCase,
    compose_flow_document,
    fake_agent_command_text,
)


class CliWorkspaceIdentityTestCase(CliHarnessTestCase):
    def assert_workspace_left_untouched(
        self, exit_code: int, stderr_text: str, expected_ledger_line_count: int
    ) -> None:
        """身份不符的 run 不得在工作区留任何痕迹（账本 / stub / 内核目录）。"""
        self.assertEqual(exit_code, 4)
        self.assertIn("不匹配", stderr_text)
        ledger_lines = (
            (self.workspace / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        )
        self.assertEqual(
            len(ledger_lines),
            expected_ledger_line_count,
            "校验失败不得向工作区账本落任何事件",
        )
        self.assertFalse((self.workspace / "AGENTS.stub.md").exists())
        for kernel_directory_name in ("audit", "packets", "snapshots"):
            self.assertFalse(
                (self.workspace / kernel_directory_name).exists(),
                f"校验失败不得新建 {kernel_directory_name}/",
            )

    def test_run_with_foreign_run_name_leaves_workspace_untouched(self) -> None:
        """回归点：拿 B 的流程跑 A 的工作区，退 4 且 A 的账本/stub/目录零变化。"""
        foreign_flow = self.test_root / "human_gate.mmd"
        self.write_human_gate_flow(foreign_flow)  # run 名 cli-human-gate
        self.write_workspace_flow_started_ledger("cli-linear", ["T001", "T002"])
        # 预置一份带用户标记的 todo.md：B 的流程若被推进，标记会被落成 run='cli-human-gate'
        # 的 user_directive 污染 A 的账本（修复前的实际现象）。
        todo_path = self.workspace / "todo.md"
        user_todo_text = "- [ ] T002 <!-- @pause -->\n"
        todo_path.write_text(user_todo_text, encoding="utf-8")

        exit_code, _stdout_text, stderr_text = self.invoke_cli(
            ["run", str(foreign_flow), "--workspace", str(self.workspace)]
        )
        self.assert_workspace_left_untouched(
            exit_code, stderr_text, expected_ledger_line_count=1
        )
        self.assertEqual(
            todo_path.read_text(encoding="utf-8"),
            user_todo_text,
            "身份不符不得改写用户已有的 todo.md",
        )

    def test_run_with_appended_node_flow_leaves_workspace_untouched(self) -> None:
        """回归点：同 run 名但图多一个节点（追加），退 4 且工作区零变化。"""
        extended_flow = self.test_root / "linear_extended.mmd"
        extended_flow.write_text(
            compose_flow_document(
                "cli-linear",
                {"fake-task": fake_agent_command_text("fake_delivery_agent.py")},
                [
                    "  T001[准备数据;;agent=fake-task;;out=data/input.txt] --> T002[生成报告;;agent=fake-task;;out=report/out.txt]",
                    "  T002 --> T003[额外收尾;;agent=fake-task;;out=extra.txt]",
                ],
            ),
            encoding="utf-8",
        )
        self.write_workspace_flow_started_ledger("cli-linear", ["T001", "T002"])

        exit_code, _stdout_text, stderr_text = self.invoke_cli(
            ["run", str(extended_flow), "--workspace", str(self.workspace)]
        )
        self.assert_workspace_left_untouched(
            exit_code, stderr_text, expected_ledger_line_count=1
        )
        self.assertFalse((self.workspace / "todo.md").exists())
        self.assertIn("T003", stderr_text, "诊断应指出多出来的节点")

    def test_run_still_cold_starts_on_empty_workspace(self) -> None:
        """回归点：身份校验前移不得误伤冷启动（空账本/不存在的目录仍能跑）。"""
        flow_path = self.test_root / "linear.mmd"
        self.write_linear_flow(flow_path)
        exit_code, _stdout_text, _stderr_text = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue((self.workspace / "AGENTS.stub.md").exists())


if __name__ == "__main__":
    unittest.main()
