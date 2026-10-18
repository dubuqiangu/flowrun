"""CLI IO 错误分类测试：读流程文件失败 与 工作区写盘失败 必须分开诊断。

回归点：改前 main 用单一 ``except OSError`` 兜底，写盘失败（账本 / todo / packet /
AGENTS.stub 等）也被报成"无法读取流程文件"（误导用户去查流程文件）。现在：

- 读流程文件失败 → stderr 明说"无法读取流程文件"；
- 工作区写盘失败 → stderr 明说"写盘失败"，且**不含**"读取"字样；
- 两者都沿用退出码 3（既有语义表：编译·校验·IO 失败），都不打 traceback。

直跑：python -m unittest discover -s tests
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIRECTORY.parent
SOURCE_ROOT = PROJECT_ROOT / "src"
for path_entry in (str(SOURCE_ROOT), str(TESTS_DIRECTORY)):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)

from flowrun.cli import main as cli_main  # noqa: E402

FLOW_WRITE_FAILURE_RUN_NAME = "cli-io-write-failure"


def compose_single_task_flow(flow_path: Path, run_name: str) -> None:
    """一份最小可编译的直线流（单任务，假 agent 命令用 echo——本测试不触发投递）。"""
    flow_path.write_text(
        "---\n"
        f"run: {run_name}\n"
        "defaults: {agent: fake-task, budget_tokens: 1000}\n"
        "agents:\n"
        "  fake-task: {cmd: 'echo hi'}\n"
        "---\n"
        "flowchart TD\n"
        "  T001[准备数据;;agent=fake-task;;out=stage-out.txt]\n",
        encoding="utf-8",
    )


class CliIoErrorClassificationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_cli_io_test_")
        self.test_root = Path(self._temp_dir.name)
        self.workspace = self.test_root / "workspace"

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def invoke_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout_capture),
            contextlib.redirect_stderr(stderr_capture),
        ):
            exit_code = cli_main(argv)
        return exit_code, stdout_capture.getvalue(), stderr_capture.getvalue()

    # ---------------------------------------------------------------- 读流程文件失败

    def test_missing_flow_file_reports_read_failure(self) -> None:
        exit_code, _stdout, stderr_text = self.invoke_cli(
            ["compile", str(self.test_root / "does-not-exist.mmd")]
        )
        self.assertEqual(exit_code, 3, "读流程文件失败沿用退出码 3")
        self.assertNotIn("Traceback", stderr_text)
        self.assertIn("无法读取流程文件", stderr_text)
        self.assertNotIn("写盘失败", stderr_text, "读失败不得误报成写盘失败")

    # ---------------------------------------------------------------- 工作区写盘失败

    def test_workspace_write_failure_reports_write_not_read(self) -> None:
        """让 stub 的落盘目标被一个同名目录占住：写文件到目录路径 → OSError。

        该失败发生在身份校验与 todo 标记之后、generate_agents_stub 写盘时，是明确的
        "工作区写盘"路径；断言 stderr 不再声称"读取"。
        """
        flow_path = self.test_root / "write_failure.mmd"
        compose_single_task_flow(flow_path, FLOW_WRITE_FAILURE_RUN_NAME)
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "AGENTS.stub.md").mkdir()  # 占住写盘目标

        exit_code, stdout_text, stderr_text = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(self.workspace)]
        )

        self.assertEqual(exit_code, 3, "工作区写盘失败沿用退出码 3")
        self.assertNotIn("Traceback", stderr_text)
        self.assertIn("写盘失败", stderr_text)
        self.assertNotIn("读取", stderr_text, "写盘失败不得再声称无法读取流程文件")
        self.assertEqual(stdout_text, "", "失败于写盘，不应打印流程状态摘要")

    def test_read_failure_and_write_failure_use_distinct_messages(self) -> None:
        """同一子命令空间下，两类 IO 失败的 stderr 文案互不相同。"""
        read_code, _read_out, read_err = self.invoke_cli(
            ["compile", str(self.test_root / "missing-flow.mmd")]
        )

        flow_path = self.test_root / "write_failure.mmd"
        compose_single_task_flow(flow_path, FLOW_WRITE_FAILURE_RUN_NAME)
        self.workspace.mkdir(parents=True, exist_ok=True)
        (self.workspace / "AGENTS.stub.md").mkdir()
        write_code, _write_out, write_err = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(self.workspace)]
        )

        self.assertEqual(read_code, 3)
        self.assertEqual(write_code, 3)
        self.assertIn("无法读取流程文件", read_err)
        self.assertIn("写盘失败", write_err)
        self.assertNotIn("读取", write_err, "写盘失败不得套用读流程文件的文案")
        self.assertNotEqual(read_err, write_err)


if __name__ == "__main__":
    unittest.main()
