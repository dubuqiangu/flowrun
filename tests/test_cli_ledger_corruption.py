"""CLI 账本损坏测试（退出码 6）：run/status/todo/resume/approve 一律退 6 且无 traceback。

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


class CliLedgerCorruptionTestCase(CliHarnessTestCase):
    def corrupt_workspace_ledger_by_appending_garbage(self, flow_path: Path) -> int:
        """先跑一次真流程产出合法账本，再追加一行垃圾制造中段损坏；返回合法行数。"""
        self.invoke_cli(["run", str(flow_path), "--workspace", str(self.workspace)])
        ledger_path = self.workspace / "ledger.jsonl"
        valid_line_count = len(ledger_path.read_text(encoding="utf-8").splitlines())
        with ledger_path.open("a", encoding="utf-8") as ledger_handle:
            ledger_handle.write("{这不是一行合法的 ledger 条目\n")
        return valid_line_count

    def assert_ledger_corruption_reported(self, argv: list[str]) -> None:
        exit_code, _stdout_text, stderr_text = self.invoke_cli(argv)
        self.assertEqual(exit_code, 6, f"{argv[0]} 应以退出码 6 报告账本损坏")
        self.assertNotIn("Traceback", stderr_text, "账本损坏不得打 Python traceback")
        self.assertIn("账本损坏", stderr_text)
        self.assertIn("ledger.jsonl", stderr_text, "诊断必须指出是哪个账本文件")

    def test_ledger_corruption_reports_exit_six_across_all_subcommands(self) -> None:
        """回归点：账本中段损坏时 run/status/todo/resume/approve 一律退 6 且无 traceback。"""
        flow_path = self.test_root / "human_gate.mmd"
        self.write_human_gate_flow(flow_path)
        self.corrupt_workspace_ledger_by_appending_garbage(flow_path)
        workspace_text = str(self.workspace)
        for argv in (
            ["status", "--workspace", workspace_text, "--flow", str(flow_path)],
            ["todo", "--workspace", workspace_text, "--flow", str(flow_path)],
            ["resume", "--flow", str(flow_path), "--workspace", workspace_text],
            ["approve", "H001", "--flow", str(flow_path), "--workspace", workspace_text],
            ["run", str(flow_path), "--workspace", workspace_text],
        ):
            with self.subTest(subcommand=argv[0]):
                self.assert_ledger_corruption_reported(argv)

    def test_snapshot_integrity_failure_also_reports_exit_six(self) -> None:
        """快照恢复路径的损坏信号与账本解析失败同型（LedgerCorruptionError → 退 6）。

        快照点 head_hash 与账本重算哈希不符 ⇒ recover_flow_state 的防篡改核对
        抛 LedgerCorruptionError（不是另一种异常）——故 main 的单一捕获即可覆盖。
        """
        flow_path = self.test_root / "linear.mmd"
        self.write_linear_flow(flow_path)
        self.invoke_cli(["run", str(flow_path), "--workspace", str(self.workspace)])
        ledger_line_count = len(
            (self.workspace / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        )
        snapshots_directory = self.workspace / "snapshots"
        snapshots_directory.mkdir(parents=True, exist_ok=True)
        (snapshots_directory / f"snapshot_{ledger_line_count:010d}.json").write_text(
            json.dumps(
                {
                    "seq": ledger_line_count,
                    "head_hash": "0" * 64,  # 与账本真实哈希不符：伪造/损坏的快照
                    "created_ts": 0,
                    "state": {},
                }
            ),
            encoding="utf-8",
        )

        exit_code, _stdout_text, stderr_text = self.invoke_cli(
            ["status", "--workspace", str(self.workspace)]
        )
        self.assertEqual(exit_code, 6)
        self.assertNotIn("Traceback", stderr_text)
        self.assertIn("账本损坏", stderr_text)
        self.assertIn("ledger.jsonl", stderr_text)

    def test_snapshot_record_missing_state_reports_exit_six(self) -> None:
        """形状非法（缺 state 键）的快照记录经 CLI 退 6，无 traceback 且含可读原因。

        改前 recover_flow_state 直接 ``latest_record["state"]`` 抛裸 KeyError，
        穿透 main() 打 traceback（违反退出码 6 契约）。
        """
        flow_path = self.test_root / "linear.mmd"
        self.write_linear_flow(flow_path)
        self.invoke_cli(["run", str(flow_path), "--workspace", str(self.workspace)])
        ledger_line_count = len(
            (self.workspace / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        )
        snapshots_directory = self.workspace / "snapshots"
        snapshots_directory.mkdir(parents=True, exist_ok=True)
        snapshot_path = snapshots_directory / f"snapshot_{ledger_line_count:010d}.json"
        snapshot_path.write_text(
            json.dumps(
                {"seq": ledger_line_count, "head_hash": "", "created_ts": 0}  # 缺 state
            ),
            encoding="utf-8",
        )

        exit_code, _stdout_text, stderr_text = self.invoke_cli(
            ["status", "--workspace", str(self.workspace)]
        )
        self.assertEqual(exit_code, 6)
        self.assertNotIn("Traceback", stderr_text)
        self.assertIn("账本损坏", stderr_text)
        self.assertIn("快照记录形状非法", stderr_text)
        self.assertIn(str(snapshot_path), stderr_text)


if __name__ == "__main__":
    unittest.main()
