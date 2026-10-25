"""CLI 装配层测试：compile 好/坏图、run 退出码口径（真子进程假 agent，零配置直跑）。

退出码契约：0=done / 1=waiting / 2=halted / 3=编译失败 / 4=工作区身份不匹配 /
5=工作区未初始化 / 6=账本损坏。

直跑：python -m unittest discover -s tests
"""

import json
import sys
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import cli_harness_support

from cli_harness_support import CliHarnessTestCase  # noqa: E402  (负责把 src/ 加进 sys.path)


class CliTestCase(CliHarnessTestCase):
    # ---------------------------------------------------------------- compile

    def test_compile_good_flow_prints_ir_json(self) -> None:
        flow_path = self.test_root / "linear.mmd"
        self.write_linear_flow(flow_path)
        exit_code, stdout_text, stderr_text = self.invoke_cli(["compile", str(flow_path)])
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr_text, "")
        compiled_ir = json.loads(stdout_text)
        self.assertEqual(compiled_ir["run_name"], "cli-linear")
        self.assertIn("T001", compiled_ir["nodes"])
        self.assertEqual(compiled_ir["nodes"]["T001"]["outputs"], ["data/input.txt"])

    def test_compile_flow_missing_run_reports_line_number_errors(self) -> None:
        flow_path = self.test_root / "bad.mmd"
        flow_path.write_text(
            "---\ndefaults: {agent: fake-task}\nagents:\n  fake-task: {cmd: 'echo hi'}\n---\n"
            "flowchart TD\n  T001[任务;;agent=fake-task;;out=a.txt]\n",
            encoding="utf-8",
        )
        exit_code, stdout_text, stderr_text = self.invoke_cli(["compile", str(flow_path)])
        self.assertEqual(exit_code, 3)
        self.assertIn("line", stderr_text)
        self.assertIn("run", stderr_text)

    def test_compile_syntax_error_reports_line_number(self) -> None:
        flow_path = self.test_root / "broken.mmd"
        flow_path.write_text(
            "---\nrun: broken\n---\nflowchat TD\n  T001[任务]\n",
            encoding="utf-8",
        )
        exit_code, _stdout_text, stderr_text = self.invoke_cli(["compile", str(flow_path)])
        self.assertEqual(exit_code, 3)
        self.assertIn("line 4", stderr_text)

    # ---------------------------------------------------------------- run

    def test_run_linear_flow_completes_with_exit_zero(self) -> None:
        flow_path = self.test_root / "linear.mmd"
        self.write_linear_flow(flow_path)
        exit_code, stdout_text, _stderr_text = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(self.workspace), "--dispatch-timeout", "60"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("flow: done", stdout_text)
        self.assertTrue((self.run_dir("cli-linear") / "data/input.txt").exists())
        self.assertTrue((self.run_dir("cli-linear") / "report/out.txt").exists())

    def test_run_stops_at_human_review_with_exit_one(self) -> None:
        flow_path = self.test_root / "human_gate.mmd"
        self.write_human_gate_flow(flow_path)
        exit_code, stdout_text, _stderr_text = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(exit_code, 1)
        self.assertIn("awaiting_human", stdout_text)
        self.assertIn("H001", stdout_text)

    def test_run_generates_agents_stub_in_workspace(self) -> None:
        flow_path = self.test_root / "linear.mmd"
        self.write_linear_flow(flow_path)
        self.invoke_cli(["run", str(flow_path), "--workspace", str(self.workspace)])
        stub_path = self.workspace / "AGENTS.stub.md"
        self.assertTrue(stub_path.exists())
        self.assertIn("data/input.txt", stub_path.read_text(encoding="utf-8"))

    def test_run_writes_flow_paused_banner_with_gate_detail(self) -> None:
        """FIX-C4：闸触发暂停后，todo 头部必须有醒目的 FLOW PAUSED + 闸/恢复提示。"""
        flow_path = self.test_root / "gate_fail.mmd"
        self.write_gate_fail_flow(flow_path)
        run_code, _run_out, _run_err = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(run_code, 1)
        todo_text = (self.run_dir("cli-gate-fail") / "todo.md").read_text(encoding="utf-8")
        self.assertIn("FLOW PAUSED", todo_text)
        self.assertIn("gate=retry", todo_text)
        self.assertIn("flowrun resume", todo_text)


if __name__ == "__main__":
    unittest.main()
