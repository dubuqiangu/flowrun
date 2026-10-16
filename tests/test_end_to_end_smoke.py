"""T013 M1 端到端冒烟：fork→join→判定→人审→收尾 的完整剧本（真子进程假 agent）。

剧本（interfaces.md §6 冒烟口径）：
1. 临时目录作 workspace，`run` 驱动到人审等待点（退出码 1）；
2. 断言 todo 投影的人审行 / packets / 审计文件 / ledger 链校验；
3. CLI `approve`（或勾选 todo 后 `run` 自动落账）→ 再 `run` → flow_done（退出码 0）；
4. 全量 replay 与恢复语义一致；判定 packet 携带上游契约核对证据。

失败注入：假 agent 不交付产物 → 节点失败 → 转人审暂停，停在可审计位置。
崩溃模拟：快照目录被删后恢复续跑，与不删快照的直跑结果等价。

直跑：python -m unittest discover -s tests
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

import sys

TESTS_DIRECTORY = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIRECTORY.parent
SOURCE_ROOT = PROJECT_ROOT / "src"
for path_entry in (str(SOURCE_ROOT), str(TESTS_DIRECTORY)):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)

from flowrun.cli import main as cli_main  # noqa: E402
from flowrun.state.flow_state import STATUS_DONE  # noqa: E402
from flowrun.state.ledger import EventLedger  # noqa: E402

FIXTURES_DIRECTORY = TESTS_DIRECTORY / "fixtures"
PYTHON_EXECUTABLE_POSIX = Path(sys.executable).as_posix()

SMOKE_FLOW_BODY_LINES = [
    "  T001[实现登录API;;agent=fake-task;;out=src/auth.py] --> J001",
    "  T002[实现登录UI;;agent=fake-task;;out=src/ui/Login.tsx] --> J001",
    "  J001[集成准备] -->|when: all| D001{验收通过?;;agent=fake-judge}",
    "  D001 -->|when: ok| H001([发布人审])",
    # 注意：fail 分支不能指回 T001——回边会让 T001 变成非入口节点，fork 起点消失；
    # 指向独立的诊断任务 T003（判定不选中则保持 planned，收口走零进展判定）。
    "  D001 -->|when: fail| T003[失败诊断;;agent=fake-task;;out=diag/report.txt]",
    "  H001 --> T004[打 tag 发布;;agent=fake-task;;out=release-tag.txt]",
]


def fake_agent_command_text(script_name: str, mode: str | None = None) -> str:
    """假 agent 命令模板（POSIX 路径 + 内层双引号 + 外层单引号，防 frontmatter 边界误判）。"""
    script_path_posix = (FIXTURES_DIRECTORY / script_name).as_posix()
    command_text = f'"{PYTHON_EXECUTABLE_POSIX}" "{script_path_posix}"'
    if mode is not None:
        command_text += f" --mode {mode}"
    return command_text


def compose_smoke_flow_document(run_name: str, task_mode: str | None = None) -> str:
    """拼装冒烟 flow.mmd：默认为成功剧本；task_mode='fail' 注入缺产物失败。"""
    agent_command_by_name = {
        "fake-task": fake_agent_command_text("fake_delivery_agent.py", mode=task_mode),
        # fake_judge_agent 的参数解析跳过 --mode 值对，packet 必须显式带 --mode hit
        # 才落在其可解析位置（该夹具属域D1，不越界改动）
        "fake-judge": fake_agent_command_text("fake_judge_agent.py", mode="hit"),
    }
    agent_declaration_lines = [
        f"  {agent_name}: {{cmd: '{command_text}'}}"
        for agent_name, command_text in agent_command_by_name.items()
    ]
    document_lines = [
        "---",
        f"run: {run_name}",
        "defaults: {agent: fake-task, budget_tokens: 100000}",
        "agents:",
        *agent_declaration_lines,
        "---",
        "flowchart TD",
        *SMOKE_FLOW_BODY_LINES,
    ]
    return "\n".join(document_lines) + "\n"


class EndToEndSmokeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_smoke_test_")
        self.test_root = Path(self._temp_dir.name)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    # ---------------------------------------------------------------- 工具

    def write_flow_file(self, run_name: str, task_mode: str | None = None) -> Path:
        flow_path = self.test_root / f"{run_name}.mmd"
        flow_path.write_text(
            compose_smoke_flow_document(run_name, task_mode=task_mode), encoding="utf-8"
        )
        return flow_path

    def make_workspace(self, workspace_name: str = "workspace") -> Path:
        return self.test_root / workspace_name

    def invoke_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout_capture),
            contextlib.redirect_stderr(stderr_capture),
        ):
            exit_code = cli_main(argv)
        return exit_code, stdout_capture.getvalue(), stderr_capture.getvalue()

    def read_todo_text(self, workspace: Path) -> str:
        return (workspace / "todo.md").read_text(encoding="utf-8")

    # ---------------------------------------------------------------- 主剧本

    def test_full_smoke_run_review_resume_then_done(self) -> None:
        """run 到人审 → approve → 再 run 到 flow_done，全程账本可重放。"""
        flow_path = self.write_flow_file("smoke-release")
        workspace = self.make_workspace()

        first_code, first_stdout, _first_err = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(workspace)]
        )
        self.assertEqual(first_code, 1, "人审等待点应退出码 1")
        self.assertIn("H001 [human] awaiting_human", first_stdout)

        todo_text = self.read_todo_text(workspace)
        self.assertIn("- [x] T001 实现登录API", todo_text)
        self.assertIn("- [x] T002 实现登录UI", todo_text)
        h001_line = next(
            line for line in todo_text.splitlines() if line.startswith("- [ ] H001")
        )
        self.assertIn("awaiting_human", h001_line)

        self.assertTrue((workspace / "packets" / "T001.packet.md").exists())
        self.assertTrue((workspace / "packets" / "D001.judge.md").exists())
        self.assertGreaterEqual(len(list(workspace.glob("*.stdout.log"))), 3)

        approve_code, _approve_out, _approve_err = self.invoke_cli(
            ["approve", "H001", "--flow", str(flow_path), "--workspace", str(workspace)]
        )
        self.assertEqual(approve_code, 0)

        resume_code, resume_stdout, _resume_err = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(workspace)]
        )
        self.assertEqual(resume_code, 0, "人审通过后续跑应 flow_done")
        self.assertIn("flow: done", resume_stdout)

        reopened_ledger = EventLedger(workspace / "ledger.jsonl")
        self.assertTrue(reopened_ledger.verify_chain())
        replayed_state = reopened_ledger.replay()
        for node_id in ("T001", "T002", "J001", "D001", "H001", "T004"):
            self.assertEqual(replayed_state.node_status(node_id), STATUS_DONE)
        self.assertEqual(
            replayed_state.node_status("T003"), "planned", "未选中的判定分支应保持 planned"
        )
        self.assertTrue(replayed_state.is_done())
        self.assertEqual(replayed_state.hop_count(), 4, "投递次数 = 两任务 + 判定 + 收尾")
        verdict_entries = [
            entry for entry in reopened_ledger.entries() if entry.type == "verdict"
        ]
        self.assertEqual(len(verdict_entries), 1)
        self.assertEqual(verdict_entries[0].payload["edge"], "H001", "判定应命中 ok 边")

        final_todo_text = self.read_todo_text(workspace)
        self.assertIn("- [x] H001 发布人审", final_todo_text)
        self.assertIn("- [x] T004 打 tag 发布", final_todo_text)
        self.assertIn("- [ ] T003 失败诊断", final_todo_text)

    def test_resume_via_todo_checkbox_edit(self) -> None:
        """勾选 todo 人审行后直接 run：run 前自动落账用户标记并续跑到完成。"""
        flow_path = self.write_flow_file("smoke-resume-checkbox")
        workspace = self.make_workspace()
        self.invoke_cli(["run", str(flow_path), "--workspace", str(workspace)])

        todo_path = workspace / "todo.md"
        todo_path.write_text(
            self.read_todo_text(workspace).replace("- [ ] H001", "- [x] H001"),
            encoding="utf-8",
        )
        resume_code, _resume_out, _resume_err = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(workspace)]
        )
        self.assertEqual(resume_code, 0)
        replayed_state = EventLedger(workspace / "ledger.jsonl").replay()
        self.assertTrue(replayed_state.is_done())
        self.assertEqual(replayed_state.node_status("T004"), STATUS_DONE)

    def test_judge_packet_carries_upstream_contract_evidence(self) -> None:
        """判定 packet 应携带 join 展开后的两路上游产物核对证据。"""
        flow_path = self.write_flow_file("smoke-evidence")
        workspace = self.make_workspace()
        self.invoke_cli(["run", str(flow_path), "--workspace", str(workspace)])

        judge_packet_text = (workspace / "packets" / "D001.judge.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("T001:src/auth.py", judge_packet_text)
        self.assertIn("T002:src/ui/Login.tsx", judge_packet_text)
        self.assertIn("存在", judge_packet_text)
        self.assertIn("D001->H001", judge_packet_text, "候选边编号必须出现在判定包")

    # ---------------------------------------------------------------- 崩溃模拟

    def test_snapshot_deletion_recovery_equals_straight_run(self) -> None:
        """中途删除快照目录：恢复续跑的最终账本与不删快照的直跑等价。"""
        flow_path = self.write_flow_file("smoke-crash")
        straight_workspace = self.make_workspace("straight")
        interrupted_workspace = self.make_workspace("interrupted")

        for workspace in (straight_workspace, interrupted_workspace):
            self.assertEqual(
                self.invoke_cli(["run", str(flow_path), "--workspace", str(workspace)])[0], 1
            )
        self.assertEqual(
            self.invoke_cli(
                ["approve", "H001", "--flow", str(flow_path), "--workspace", str(interrupted_workspace)]
            )[0],
            0,
        )
        snapshots_directory = interrupted_workspace / "snapshots"
        if snapshots_directory.exists():
            for snapshot_file in snapshots_directory.iterdir():
                snapshot_file.unlink()

        self.assertEqual(
            self.invoke_cli(["run", str(flow_path), "--workspace", str(interrupted_workspace)])[0], 0
        )
        self.assertEqual(
            self.invoke_cli(
                ["approve", "H001", "--flow", str(flow_path), "--workspace", str(straight_workspace)]
            )[0],
            0,
        )
        self.assertEqual(
            self.invoke_cli(["run", str(flow_path), "--workspace", str(straight_workspace)])[0], 0
        )

        straight_ledger = EventLedger(straight_workspace / "ledger.jsonl")
        interrupted_ledger = EventLedger(interrupted_workspace / "ledger.jsonl")
        self.assertTrue(straight_ledger.verify_chain())
        self.assertTrue(interrupted_ledger.verify_chain())
        self.assertEqual(straight_ledger.last_seq, interrupted_ledger.last_seq)
        straight_state = straight_ledger.replay()
        interrupted_state = interrupted_ledger.replay()
        for node_id in ("T001", "T002", "J001", "D001", "H001", "T003", "T004"):
            self.assertEqual(
                straight_state.node_status(node_id),
                interrupted_state.node_status(node_id),
            )
        self.assertEqual(straight_state.hop_count(), interrupted_state.hop_count())

    # ---------------------------------------------------------------- 失败注入

    def test_missing_outputs_stop_at_auditable_review_point(self) -> None:
        """任务 agent 缺产物 → 判 fail → 无重试边 → 闸触发暂停，停在可审计位置。"""
        flow_path = self.write_flow_file("smoke-fail-injection", task_mode="fail")
        workspace = self.make_workspace()
        exit_code, run_stdout, _run_err = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(workspace)]
        )
        self.assertEqual(exit_code, 1, "失败注入应停在等待点而非崩溃")

        reopened_ledger = EventLedger(workspace / "ledger.jsonl")
        self.assertTrue(reopened_ledger.verify_chain())
        node_failed_entries = [
            entry
            for entry in reopened_ledger.entries()
            if entry.type == "node_failed" and entry.node == "T001"
        ]
        self.assertTrue(node_failed_entries, "T001 失败必须落 node_failed 审计")
        gate_tripped_entries = [
            entry for entry in reopened_ledger.entries() if entry.type == "gate_tripped"
        ]
        self.assertTrue(gate_tripped_entries, "无重试边的失败应触发闸转人审")
        self.assertFalse((workspace / "src/auth.py").exists(), "失败 agent 不应交付产物")

        status_code, status_stdout, _status_err = self.invoke_cli(
            ["status", "--workspace", str(workspace), "--flow", str(flow_path)]
        )
        self.assertEqual(status_code, 0)
        self.assertIn("T001 [task] failed", status_stdout)
        self.assertIn("已触发闸", status_stdout)
        todo_text = self.read_todo_text(workspace)
        self.assertIn("T001 实现登录API", todo_text)
        self.assertIn("FLOW PAUSED", todo_text, "FIX-C4：暂停态 todo 必须标注 PAUSED")
        self.assertIn("gate=retry", todo_text)


if __name__ == "__main__":
    unittest.main()
