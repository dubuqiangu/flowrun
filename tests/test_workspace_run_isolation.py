"""T022 多 run 隔离布局语义的覆盖测试（legacy 扁平 vs namespaced 分包）。

覆盖三条用户裁定的布局语义（design.md / kernel.workspace_layout 模块 docstring）：

1. **legacy**：工作区根已有 `ledger.jsonl` → 沿用扁平路径，零迁移，
   不创建 `<workspace>/<run>/` 子目录；
2. **namespaced**：根无账本 → ledger/snapshots/packets/audit/todo 五件套进
   `<workspace>/<run>/`，`AGENTS.stub.md` 等工作区级资产留在根；
3. **同工作区多 run**：两个 run 的账本/packets/todo/audit 完全隔离，互不串扰。

直跑：python -m unittest discover -s tests
"""

import asyncio
import json
import sys
import threading
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import cli_harness_support / kernel_test_support

import kernel_test_support as support  # noqa: E402
from cli_harness_support import (  # noqa: E402  (负责把 src/ 加进 sys.path)
    CliHarnessTestCase,
    compose_flow_document,
    fake_agent_command_text,
)
from flowrun.kernel.engine import run_flow  # noqa: E402
from flowrun.state.ledger import EventLedger  # noqa: E402

LINEAR_FLOW_BODY_LINES = [
    "  T001[准备数据;;agent=fake-task;;out=data/input.txt] --> T002[生成报告;;agent=fake-task;;out=report/out.txt]",
]

# 与 kernel.workspace_layout.required_directories() 同口径的 run 内五件套
RUN_DIRECTORY_BUNDLE = ("ledger.jsonl", "snapshots", "packets", "audit", "todo.md")


class WorkspaceRunIsolationTestCase(CliHarnessTestCase):
    def write_linear_flow_with_run_name(self, flow_path: Path, run_name: str) -> None:
        """两任务直线流（无人审），run 名可指定：供同工作区多 run 隔离用。"""
        flow_path.write_text(
            compose_flow_document(
                run_name,
                {"fake-task": fake_agent_command_text("fake_delivery_agent.py")},
                LINEAR_FLOW_BODY_LINES,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def read_ledger_records(ledger_path: Path) -> list[dict]:
        """按行解析账本 JSON（不打开 EventLedger，避免句柄管理噪音）。"""
        return [
            json.loads(raw_line)
            for raw_line in ledger_path.read_text(encoding="utf-8").splitlines()
            if raw_line.strip()
        ]

    # ---------------------------------------------------------------- 语义 1

    def test_legacy_root_ledger_runs_flat_without_run_subdirectory(self) -> None:
        """根有 ledger.jsonl → 扁平运行：产物全在根，不创建 <ws>/<run>/。"""
        self.write_workspace_flow_started_ledger("cli-linear", ["T001", "T002"])
        flow_path = self.test_root / "legacy_linear.mmd"
        self.write_linear_flow_with_run_name(flow_path, "cli-linear")

        exit_code, _stdout_text, _stderr_text = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(exit_code, 0, "legacy 工作区应正常跑到 flow_done")

        # 扁平产物留在根（零迁移）
        self.assertTrue((self.workspace / "ledger.jsonl").is_file())
        self.assertTrue((self.workspace / "todo.md").is_file())
        self.assertTrue((self.workspace / "packets" / "T001.packet.md").is_file())
        self.assertTrue((self.workspace / "audit").is_dir())
        # 账本就是根上这份：flow_done 落在根账本里
        root_ledger_records = self.read_ledger_records(self.workspace / "ledger.jsonl")
        self.assertIn("flow_done", [record["type"] for record in root_ledger_records])
        # 不得静默分包出 run 子目录
        self.assertFalse(
            (self.workspace / "cli-linear").exists(),
            "legacy 工作区不得创建 <ws>/<run>/ 子目录",
        )

    # ---------------------------------------------------------------- 语义 2

    def test_namespaced_run_keeps_five_piece_bundle_in_run_directory(self) -> None:
        """根无账本 → 五件套进 <ws>/<run>/，根只留 AGENTS.stub.md 级工作区资产。"""
        flow_path = self.test_root / "namespaced_linear.mmd"
        self.write_linear_flow_with_run_name(flow_path, "layout-alpha")

        exit_code, _stdout_text, _stderr_text = self.invoke_cli(
            ["run", str(flow_path), "--workspace", str(self.workspace)]
        )
        self.assertEqual(exit_code, 0)

        run_directory = self.workspace / "layout-alpha"
        for bundle_name in RUN_DIRECTORY_BUNDLE:
            self.assertTrue(
                (run_directory / bundle_name).exists(),
                f"namespaced run 目录应含 {bundle_name}",
            )

        # 工作区根不得残留 run 级资产
        root_level_run_assets = (
            "ledger.jsonl",
            "todo.md",
            "packets",
            "audit",
            "snapshots",
        )
        for asset_name in root_level_run_assets:
            self.assertFalse(
                (self.workspace / asset_name).exists(),
                f"工作区根不应出现 run 级资产 {asset_name}",
            )
        # AGENTS.stub.md 是工作区级资产：留在根、各 run 共享
        self.assertTrue((self.workspace / "AGENTS.stub.md").is_file())
        # 账本内容属于该 run（flow_started/flow_done 的 run 字段）
        run_ledger_records = self.read_ledger_records(
            run_directory / "ledger.jsonl"
        )
        event_types = [record["type"] for record in run_ledger_records]
        self.assertIn("flow_done", event_types)
        self.assertEqual(
            {record["run"] for record in run_ledger_records}, {"layout-alpha"}
        )

    # ---------------------------------------------------------------- 语义 3

    def test_two_runs_in_one_workspace_have_fully_isolated_state(self) -> None:
        """同工作区先后跑两个 run：账本/packets/todo/audit 各归各，互不串扰。"""
        first_flow_path = self.test_root / "first_run.mmd"
        second_flow_path = self.test_root / "second_run.mmd"
        self.write_linear_flow_with_run_name(first_flow_path, "iso-first")
        self.write_linear_flow_with_run_name(second_flow_path, "iso-second")

        self.assertEqual(
            self.invoke_cli(
                ["run", str(first_flow_path), "--workspace", str(self.workspace)]
            )[0],
            0,
        )
        self.assertEqual(
            self.invoke_cli(
                ["run", str(second_flow_path), "--workspace", str(self.workspace)]
            )[0],
            0,
            "第二个 run 不得被第一个 run 的身份校验拒绝",
        )

        first_run_directory = self.workspace / "iso-first"
        second_run_directory = self.workspace / "iso-second"
        self.assertTrue(first_run_directory.is_dir())
        self.assertTrue(second_run_directory.is_dir())

        # 账本隔离：各自只记自己的 run，事件互不可见
        first_ledger = EventLedger(first_run_directory / "ledger.jsonl")
        try:
            first_entries = first_ledger.entries()
        finally:
            first_ledger.close()
        second_ledger = EventLedger(second_run_directory / "ledger.jsonl")
        try:
            second_entries = second_ledger.entries()
        finally:
            second_ledger.close()
        self.assertTrue(first_entries, "第一个 run 的账本应有事件")
        self.assertTrue(second_entries, "第二个 run 的账本应有事件")
        self.assertEqual({entry.run for entry in first_entries}, {"iso-first"})
        self.assertEqual({entry.run for entry in second_entries}, {"iso-second"})

        # packets/todo/audit 隔离：run A 目录里没有 run B 的痕迹，反之亦然
        self.assertTrue((first_run_directory / "packets" / "T001.packet.md").is_file())
        self.assertTrue((second_run_directory / "packets" / "T001.packet.md").is_file())
        self.assertTrue((first_run_directory / "todo.md").is_file())
        self.assertTrue((second_run_directory / "todo.md").is_file())
        self.assertTrue((first_run_directory / "audit").is_dir())
        self.assertTrue((second_run_directory / "audit").is_dir())
        # 根仍是无账本的工作区根：不因多 run 退化成 legacy 扁平
        self.assertFalse((self.workspace / "ledger.jsonl").exists())
        self.assertFalse((self.workspace / "todo.md").exists())

    # ---------------------------------------------------------------- 并发（T022 验收原文）

    def test_concurrent_runs_in_one_workspace_do_not_interfere(self) -> None:
        """T022 验收：同工作区两个 run **并发**执行互不串扰。

        两个线程经 barrier 同时起步，各自 ``asyncio.run(run_flow(...))``、各自一套
        独立 ``KernelHookStubs``（线程不共享 hook 状态）。断言：两者都 is_done()；
        线程内无异常；两份账本只含自己 run 的事件（run 字段集合）；工作区根无
        ledger.jsonl；packets/todo/audit 各归各 run 目录。
        """
        def make_flow_ir_for_run(run_name: str):
            """两个 run 用结构相同、对象独立的图（同构才能验证隔离而非差异）。"""
            return support.make_flow_ir(
                [support.make_node("T001"), support.make_node("T002")],
                [support.make_edge("T001", "T002")],
                run_name=run_name,
            )

        flow_ir_alpha = make_flow_ir_for_run("concurrent-alpha")
        flow_ir_beta = make_flow_ir_for_run("concurrent-beta")

        final_states: dict[str, object] = {}
        thread_errors: dict[str, BaseException] = {}
        start_barrier = threading.Barrier(2)

        def run_flow_in_thread(run_name: str, flow_ir, stubs) -> None:
            try:
                start_barrier.wait(timeout=10)
                final_states[run_name] = asyncio.run(
                    run_flow(flow_ir, self.workspace, stubs.build_hooks())
                )
            except BaseException as exc:  # 并发用例必须如实暴露线程内异常
                thread_errors[run_name] = exc

        worker_threads = [
            threading.Thread(
                target=run_flow_in_thread,
                args=("concurrent-alpha", flow_ir_alpha, support.KernelHookStubs()),
                name="run-concurrent-alpha",
            ),
            threading.Thread(
                target=run_flow_in_thread,
                args=("concurrent-beta", flow_ir_beta, support.KernelHookStubs()),
                name="run-concurrent-beta",
            ),
        ]
        for worker_thread in worker_threads:
            worker_thread.start()
        for worker_thread in worker_threads:
            worker_thread.join(timeout=60)

        self.assertEqual(
            thread_errors, {}, f"并发 run 不得抛异常: {thread_errors}"
        )
        for worker_thread in worker_threads:
            self.assertFalse(
                worker_thread.is_alive(), f"{worker_thread.name} 未在超时内结束"
            )
        for run_name in ("concurrent-alpha", "concurrent-beta"):
            self.assertIn(run_name, final_states, f"{run_name} 应产出终态")
            self.assertTrue(
                final_states[run_name].is_done(), f"{run_name} 应跑到 flow_done"
            )

        # 两份账本各自只含自己 run 的事件
        alpha_run_directory = self.workspace / "concurrent-alpha"
        beta_run_directory = self.workspace / "concurrent-beta"
        alpha_records = self.read_ledger_records(alpha_run_directory / "ledger.jsonl")
        beta_records = self.read_ledger_records(beta_run_directory / "ledger.jsonl")
        self.assertTrue(alpha_records)
        self.assertTrue(beta_records)
        self.assertEqual({record["run"] for record in alpha_records}, {"concurrent-alpha"})
        self.assertEqual({record["run"] for record in beta_records}, {"concurrent-beta"})
        alpha_types = [record["type"] for record in alpha_records]
        beta_types = [record["type"] for record in beta_records]
        self.assertIn("flow_done", alpha_types)
        self.assertIn("flow_done", beta_types)

        # packets / todo / audit 各归各 run 目录；工作区根无 ledger.jsonl
        # （packet 文件名由 hook 实现决定：KernelHookStubs 写 T001.md、真
        # packet_builder 写 T001.packet.md——这里断言 T001 的 packet 存在即可）
        for run_directory in (alpha_run_directory, beta_run_directory):
            packet_paths = list((run_directory / "packets").glob("T001*.md"))
            self.assertTrue(
                packet_paths, f"{run_directory.name} 的 packets/ 应有 T001 的 packet"
            )
            self.assertTrue((run_directory / "todo.md").is_file())
            self.assertTrue((run_directory / "audit").is_dir())
        self.assertFalse((self.workspace / "ledger.jsonl").exists())
        self.assertFalse((self.workspace / "packets").exists())
        self.assertFalse((self.workspace / "audit").exists())
        self.assertFalse((self.workspace / "todo.md").exists())


if __name__ == "__main__":
    unittest.main()
