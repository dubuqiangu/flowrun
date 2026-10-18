"""CLI resume 通道测试（FIX-C1）：暂停解除、身份校验、退出码 1/4/5 口径。

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
from flowrun.state.ledger import EventLedger  # noqa: E402


class CliResumeTestCase(CliHarnessTestCase):
    def test_resume_after_gate_pause_writes_flow_resumed(self) -> None:
        """闸触发挂起 → resume 落 flow_resumed(by=cli) 并解除暂停。"""
        flow_path = self.test_root / "gate_fail.mmd"
        self.write_gate_fail_flow(flow_path)
        run_code, run_stdout, _run_err = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(run_code, 1)
        self.assertIn("flow: paused", run_stdout)

        resume_code, resume_stdout, resume_err = self.invoke_cli(
            ["resume", "--flow", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(resume_code, 0)
        self.assertEqual(resume_err, "")
        self.assertIn("已恢复", resume_stdout)

        ledger_lines = (
            (self.workspace / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        )
        resumed_lines = [line for line in ledger_lines if '"flow_resumed"' in line]
        self.assertEqual(len(resumed_lines), 1, "resume 只落一条 flow_resumed")
        self.assertIn('"by":"cli"', resumed_lines[0])

        _status_code, status_stdout, _status_err = self.invoke_cli(
            ["status", "--workspace", str(self.workspace), "--flow", str(flow_path)]
        )
        self.assertIn("flow: in-progress", status_stdout, "恢复后不再是暂停态")

    def test_resume_enables_progress_to_done(self) -> None:
        """暂停的流经 resume 后能真正推进到 done（不 resume 时 run 只会停在暂停态）。"""
        flow_path = self.test_root / "human_gate.mmd"
        self.write_human_gate_flow(flow_path)
        self.assertEqual(
            self.invoke_cli(["run", str(flow_path), "--workspace", str(self.workspace)])[0],
            1,
        )

        # 用 todo 行尾注释 @pause 把流挂起（T002 此时 planned，可被暂停）
        todo_path = self.workspace / "todo.md"
        todo_path.write_text(
            todo_path.read_text(encoding="utf-8").replace(
                "- [ ] T002", "- [ ] T002 <!-- @pause -->"
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            self.invoke_cli(
                ["todo", "--workspace", str(self.workspace), "--flow", str(flow_path)]
            )[0],
            0,
        )
        _paused_code, paused_stdout, _paused_err = self.invoke_cli(
            ["status", "--workspace", str(self.workspace), "--flow", str(flow_path)]
        )
        self.assertIn("flow: paused", paused_stdout)

        # 人审放行 + resume 解除暂停，再 run 应能推进到完成
        self.assertEqual(
            self.invoke_cli(
                ["approve", "H001", "--flow", str(flow_path), "--workspace", str(self.workspace)]
            )[0],
            0,
        )
        self.assertEqual(
            self.invoke_cli(
                ["resume", "--flow", str(flow_path), "--workspace", str(self.workspace)]
            )[0],
            0,
        )
        resume_run_code, resume_run_stdout, _err = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(resume_run_code, 0)
        self.assertIn("flow: done", resume_run_stdout)

    def test_resume_refused_when_not_paused(self) -> None:
        """停在人审等待点（非暂停）时 resume 拒绝：退出码 1，不落 flow_resumed。"""
        flow_path = self.test_root / "human_gate.mmd"
        self.write_human_gate_flow(flow_path)
        self.invoke_cli(["run", str(flow_path), "--workspace", str(self.workspace)])
        resume_code, _resume_out, resume_err = self.invoke_cli(
            ["resume", "--flow", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(resume_code, 1)
        self.assertIn("未处于暂停状态", resume_err)
        ledger_text = (self.workspace / "ledger.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("flow_resumed", ledger_text)

    def test_resume_refused_when_done(self) -> None:
        """已完成的流拒绝 resume：退出码 1。"""
        flow_path = self.test_root / "linear.mmd"
        self.write_linear_flow(flow_path)
        self.assertEqual(
            self.invoke_cli(
                ["run", str(flow_path), "--workspace", str(self.workspace)]
            )[0],
            0,
        )
        resume_code, _resume_out, resume_err = self.invoke_cli(
            ["resume", "--flow", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(resume_code, 1)
        self.assertIn("已完成", resume_err)

    def test_resume_workspace_identity_mismatch_exits_four(self) -> None:
        """工作区账本 run 与图 run 不符 → 退出码 4（工作区身份不匹配）。"""
        linear_flow = self.test_root / "linear.mmd"
        self.write_linear_flow(linear_flow)  # run 名 cli-linear
        self.invoke_cli(["run", str(linear_flow), "--workspace", str(self.workspace)])

        other_flow = self.test_root / "human_gate.mmd"
        self.write_human_gate_flow(other_flow)  # run 名 cli-human-gate
        resume_code, _resume_out, resume_err = self.invoke_cli(
            ["resume", "--flow", str(other_flow), "--workspace", str(self.workspace)]
        )
        self.assertEqual(resume_code, 4)
        self.assertIn("不匹配", resume_err)

    def test_resume_on_missing_workspace_exits_five(self) -> None:
        """工作区未初始化时 resume 退 5（与 status/todo 口径一致）。"""
        flow_path = self.test_root / "linear.mmd"
        self.write_linear_flow(flow_path)
        exit_code, _stdout_text, stderr_text = self.invoke_cli(
            [
                "resume",
                "--flow",
                str(flow_path),
                "--workspace",
                str(self.test_root / "nope"),
            ]
        )
        self.assertEqual(exit_code, 5)
        self.assertIn("工作区未初始化", stderr_text)

    def test_resume_on_directory_without_ledger_exits_five(self) -> None:
        """目录存在但缺账本 → 仍是"未初始化"退 5（不因走了内核校验而改口径）。"""
        flow_path = self.test_root / "linear.mmd"
        self.write_linear_flow(flow_path)
        self.workspace.mkdir(parents=True, exist_ok=True)
        exit_code, _stdout_text, stderr_text = self.invoke_cli(
            ["resume", "--flow", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(exit_code, 5)
        self.assertIn("工作区未初始化", stderr_text)

    def test_resume_same_run_name_with_appended_node_exits_four(self) -> None:
        """回归点：同 run 名但图多一个节点 → resume 退 4（改前只比 run 名会放行）。"""
        self.write_workspace_flow_started_ledger("cli-linear", ["T001", "T002"])
        appended_flow = self.test_root / "appended.mmd"
        appended_flow.write_text(
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

        exit_code, _stdout_text, stderr_text = self.invoke_cli(
            ["resume", "--flow", str(appended_flow), "--workspace", str(self.workspace)]
        )
        self.assertEqual(exit_code, 4)
        self.assertIn("不匹配", stderr_text)
        self.assertIn("T003", stderr_text, "诊断应指出多出来的节点")
        self.assertNotIn(
            "flow_resumed",
            (self.workspace / "ledger.jsonl").read_text(encoding="utf-8"),
            "身份不符不得落 flow_resumed",
        )

    def test_resume_same_run_name_with_missing_node_exits_four(self) -> None:
        """回归点：同 run 名但计划节点在当前图中消失 → resume 退 4。"""
        self.write_workspace_flow_started_ledger("cli-linear", ["T001", "T002"])
        reduced_flow = self.test_root / "reduced.mmd"
        reduced_flow.write_text(
            compose_flow_document(
                "cli-linear",
                {"fake-task": fake_agent_command_text("fake_delivery_agent.py")},
                ["  T001[准备数据;;agent=fake-task;;out=data/input.txt]"],
            ),
            encoding="utf-8",
        )

        exit_code, _stdout_text, stderr_text = self.invoke_cli(
            ["resume", "--flow", str(reduced_flow), "--workspace", str(self.workspace)]
        )
        self.assertEqual(exit_code, 4)
        self.assertIn("不匹配", stderr_text)
        self.assertIn("T002", stderr_text, "诊断应指出消失的节点")

    def test_resume_equal_node_set_on_paused_flow_exits_zero(self) -> None:
        """节点集相等 → 身份校验放行；暂停态 resume 正常返回 0 并落 flow_resumed。"""
        flow_path = self.test_root / "gate_fail.mmd"
        self.write_gate_fail_flow(flow_path)
        self.assertEqual(
            self.invoke_cli(["run", str(flow_path), "--workspace", str(self.workspace)])[0],
            1,
        )

        exit_code, stdout_text, stderr_text = self.invoke_cli(
            ["resume", "--flow", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stderr_text, "")
        self.assertIn("已恢复", stdout_text)
        self.assertIn(
            "flow_resumed",
            (self.workspace / "ledger.jsonl").read_text(encoding="utf-8"),
        )

    def test_resume_legacy_ledger_without_planned_nodes_is_rejected(self) -> None:
        """缺陷 1（fail-closed）：老账本 flow_started 无 planned_nodes → 退 4。

        旧行为只校验 run 名（fail-open）放行后由非暂停态退 1；现改为身份不可校验
        即拒绝：诊断须说明缺 planned_nodes 并建议换新工作区。

        说明：这是**纠正前置条件**（拒绝而非跳过校验），不是放宽断言。"""
        self.workspace.mkdir(parents=True, exist_ok=True)
        ledger = EventLedger(self.workspace / "ledger.jsonl")
        try:
            ledger.append(
                "flow_started",
                {"entry_nodes": ["T001"]},  # 老账本：无 planned_nodes
                "cli-linear-flow-started",
                "cli-linear",
            )
        finally:
            ledger.close()

        flow_path = self.test_root / "linear.mmd"
        self.write_linear_flow(flow_path)  # 同 run 名 cli-linear，节点 T001/T002
        exit_code, _stdout_text, stderr_text = self.invoke_cli(
            ["resume", "--flow", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(exit_code, 4, "计划节点集缺失 = 身份不可校验 → 退 4")
        self.assertIn("planned_nodes", stderr_text)
        self.assertIn("换新工作区", stderr_text)


if __name__ == "__main__":
    unittest.main()
