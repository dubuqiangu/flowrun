"""样例项目（examples/demo）的回归测试：样例必须一直能跑通。

样例是仓库对外的"可运行说明书"，一旦引擎侧行为漂移而样例没人跑，README 就会过期。
本测试直接驱动 ``examples/demo/run_demo.py`` 里的剧本构造函数与 CLI，覆盖：

- 成功剧本：compile → run 停在人审 → approve → 再 run 到 flow_done；
- 失败路径 A：agent 自称做完却不交产物 → 节点失败 + retry 闸；
- 失败路径 B：判定 agent 回不存在的边 → verdict 边为 None + verdict 闸。

样例命令是临时拼的绝对路径（适配器以 workspace 为 cwd 拉起子进程），
因此本测试不把任何机器路径固化进仓库。
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIRECTORY.parent
SOURCE_ROOT = PROJECT_ROOT / "src"
DEMO_DIRECTORY = PROJECT_ROOT / "examples" / "demo"
for path_entry in (str(SOURCE_ROOT), str(DEMO_DIRECTORY)):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)

import run_demo  # noqa: E402
from flowrun.cli import main as cli_main  # noqa: E402


def read_ledger_records(workspace: Path) -> list[dict]:
    ledger_path = workspace / "ledger.jsonl"
    return [
        json.loads(raw_line)
        for raw_line in ledger_path.read_text(encoding="utf-8").splitlines()
        if raw_line.strip()
    ]


def event_types_of(workspace: Path) -> list[str]:
    return [record.get("type") for record in read_ledger_records(workspace)]


class DemoProjectTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_demo_test_")
        self.test_root = Path(self._temp_dir.name)
        self.created_workspaces: list[Path] = []

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def invoke_cli(self, argv: list[str]) -> int:
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return cli_main(argv)

    def build_flow(
        self,
        file_name: str,
        worker_extra_arguments: str = "",
        judge_extra_arguments: str = "",
    ) -> Path:
        return run_demo.write_demo_flow(
            self.test_root / file_name,
            worker_extra_arguments=worker_extra_arguments,
            judge_extra_arguments=judge_extra_arguments,
        )

    def run_flow(self, flow_path: Path, workspace_name: str) -> tuple[int, Path]:
        workspace = self.test_root / workspace_name
        exit_code = self.invoke_cli(["run", str(flow_path), "--workspace", str(workspace)])
        return exit_code, workspace

    def test_demo_flow_compiles(self) -> None:
        flow_path = self.build_flow("demo.mmd")
        self.assertEqual(self.invoke_cli(["compile", str(flow_path)]), 0)
        self.assertIn("demo-worker", flow_path.read_text(encoding="utf-8"))

    def test_success_story_stops_at_human_then_finishes(self) -> None:
        flow_path = self.build_flow("demo.mmd")
        first_exit_code, workspace = self.run_flow(flow_path, "workspace_review")

        self.assertEqual(first_exit_code, 1)
        self.assertIn("human_requested", event_types_of(workspace))
        for product_relative_path in ("src/auth.py", "tests/test_auth.py", "src/ui/Login.jsx"):
            self.assertTrue((workspace / product_relative_path).is_file())
        self.assertTrue((workspace / "audit" / "T001-agent-1.stdout.log").is_file())
        self.assertTrue((workspace / "audit" / "D001-judge-1.stdout.log").is_file())

        self.assertEqual(
            self.invoke_cli(
                ["approve", "H001", "--flow", str(flow_path), "--workspace", str(workspace)]
            ),
            0,
        )
        second_exit_code = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(workspace)]
        )
        self.assertEqual(second_exit_code, 0)
        self.assertEqual(event_types_of(workspace)[-1], "flow_done")
        self.assertTrue((workspace / "release-tag.txt").is_file())

    def test_missing_outputs_trip_retry_gate(self) -> None:
        flow_path = self.build_flow("demo_fail.mmd", worker_extra_arguments="--mode lie")
        exit_code, workspace = self.run_flow(flow_path, "workspace_missing")

        self.assertEqual(exit_code, 1)
        recorded_types = event_types_of(workspace)
        self.assertIn("node_failed", recorded_types)
        self.assertIn("gate_tripped", recorded_types)
        self.assertFalse((workspace / "src" / "auth.py").exists())
        failed_node_payload = next(
            record["payload"]
            for record in read_ledger_records(workspace)
            if record.get("type") == "node_failed"
        )
        self.assertEqual(failed_node_payload.get("reason"), "missing_outputs")

    def test_illegal_judge_edge_trips_verdict_gate(self) -> None:
        flow_path = self.build_flow("demo_miss.mmd", judge_extra_arguments="--mode miss")
        exit_code, workspace = self.run_flow(flow_path, "workspace_miss")

        self.assertEqual(exit_code, 1)
        verdict_record = next(
            record
            for record in read_ledger_records(workspace)
            if record.get("type") == "verdict"
        )
        self.assertIsNone(verdict_record["payload"]["edge"])
        self.assertTrue(verdict_record["payload"]["tripped"])
        self.assertIn("gate_tripped", event_types_of(workspace))

    def test_agent_process_failure_records_nonzero_exit(self) -> None:
        flow_path = self.build_flow("demo_crash.mmd", worker_extra_arguments="--mode crash")
        exit_code, workspace = self.run_flow(flow_path, "workspace_crash")

        self.assertEqual(exit_code, 1)
        failure_reasons = {
            record["payload"].get("reason")
            for record in read_ledger_records(workspace)
            if record.get("type") == "node_failed"
        }
        self.assertEqual(failure_reasons, {"nonzero_exit"})


if __name__ == "__main__":
    unittest.main()