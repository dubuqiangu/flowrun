"""样例项目的驱动脚本：把 flow.mmd 里的命令占位符换成真实绝对路径，再跑完整剧本。

为什么要这个脚本：适配器是**以 workspace 为 cwd** 拉起 agent 子进程的，而
`cmd:` 只支持 `__FLOW_PACKET__` / `__FLOW_PACKET_PATH__` 两个占位符，没有"仓库根"。
所以样例命令必须写成绝对路径——本脚本负责生成，避免把某台机器的路径固化进仓库。

用法：
    python examples/demo/run_demo.py           # 完整剧本，跑完自己删临时目录
    python examples/demo/run_demo.py --keep    # 保留临时目录，方便自己翻看工作区

剧本（每一步都打印命令、输出与退出码，最后给验收表）：
    [0] compile  → 编译样例流程图
    [1] run      → 两个任务并行产出 → join → 判定 ok → 停在人审（exit 1）
    [2] status / todo → 看两个监控面
    [3] approve  → 再 run → flow_done（exit 0）
    [4] 失败路径 A：agent 自称做完但不写产物 → missing_outputs → retry 闸（exit 1）
    [5] 失败路径 B：判定 agent 回不存在的边 → 非法边被拒 → verdict 闸（exit 1）
    [6] 失败路径 C：agent 进程级失败（退出码非 0）→ nonzero_exit（exit 1）

脚本会根据账本与工作区自行断言，全绿才算跑通（退出码 0）。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEMO_DIRECTORY = Path(__file__).resolve().parent
REPOSITORY_ROOT = DEMO_DIRECTORY.parent.parent
FLOW_TEMPLATE_PATH = DEMO_DIRECTORY / "flow.mmd"
AGENT_SCRIPT_PATH = DEMO_DIRECTORY / "demo_agent.py"
JUDGE_SCRIPT_PATH = DEMO_DIRECTORY / "demo_judge.py"
PYTHON_EXECUTABLE_POSIX = Path(sys.executable).as_posix()
PLACEHOLDER_WORKER = "__DEMO_WORKER_CMD__"
PLACEHOLDER_JUDGE = "__DEMO_JUDGE_CMD__"

EXIT_CODE_DONE = 0
EXIT_CODE_WAITING = 1

CHECKS: list[tuple[str, bool]] = []


def record_check(description: str, passed: bool) -> None:
    CHECKS.append((description, passed))
    print(f"  [{'OK' if passed else 'FAIL'}] {description}", flush=True)


def build_demo_flow_text(
    worker_extra_arguments: str = "", judge_extra_arguments: str = ""
) -> str:
    """把模板里的两个命令占位符替换成绝对路径命令。"""
    worker_command = f"{PYTHON_EXECUTABLE_POSIX} {AGENT_SCRIPT_PATH.as_posix()}"
    if worker_extra_arguments:
        worker_command += " " + worker_extra_arguments
    judge_command = f"{PYTHON_EXECUTABLE_POSIX} {JUDGE_SCRIPT_PATH.as_posix()}"
    if judge_extra_arguments:
        judge_command += " " + judge_extra_arguments
    else:
        judge_command += " --mode hit"
    template_text = FLOW_TEMPLATE_PATH.read_text(encoding="utf-8")
    return template_text.replace(PLACEHOLDER_WORKER, worker_command).replace(
        PLACEHOLDER_JUDGE, judge_command
    )


def write_demo_flow(
    destination_path: Path, worker_extra_arguments: str = "", judge_extra_arguments: str = ""
) -> Path:
    destination_path.write_text(
        build_demo_flow_text(worker_extra_arguments, judge_extra_arguments),
        encoding="utf-8",
        newline="\n",
    )
    return destination_path


def run_flowrun(arguments: list[str]) -> int:
    """以仓库为 cwd 调 CLI（PYTHONPATH 指向 src，无需 pip install）。"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "src")
    print("\n$ flowrun " + " ".join(arguments), flush=True)
    completed = subprocess.run(
        [sys.executable, "-m", "flowrun.cli", *arguments],
        cwd=str(REPOSITORY_ROOT),
        env=environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if completed.stdout:
        print(completed.stdout.rstrip(), flush=True)
    if completed.stderr:
        print("[stderr] " + completed.stderr.rstrip(), flush=True)
    print(f"→ exit={completed.returncode}", flush=True)
    return completed.returncode


def list_workspace_files(workspace_directory: Path) -> None:
    print("\n工作区文件：", flush=True)
    for file_path in sorted(workspace_directory.rglob("*")):
        if file_path.is_file():
            relative_path = file_path.relative_to(workspace_directory).as_posix()
            print("  " + relative_path, flush=True)


def read_ledger_records(workspace_directory: Path) -> list[dict]:
    ledger_path = workspace_directory / "ledger.jsonl"
    if not ledger_path.exists():
        return []
    return [
        json.loads(raw_line)
        for raw_line in ledger_path.read_text(encoding="utf-8").splitlines()
        if raw_line.strip()
    ]


def describe_ledger(records: list[dict]) -> str:
    """把事件压成一行，让人一眼看出流程走过哪些步。"""
    parts = []
    for record in records:
        node_number = record.get("node")
        parts.append(record.get("type", "?") + (f"[{node_number}]" if node_number else ""))
    return " → ".join(parts)


def print_todo_human_line(workspace_directory: Path) -> str:
    todo_text = (workspace_directory / "todo.md").read_text(encoding="utf-8")
    print("\ntodo.md 里的人审行：", flush=True)
    human_line = ""
    for todo_line in todo_text.splitlines():
        if "H001" in todo_line:
            print("  " + todo_line, flush=True)
            human_line = todo_line
    return human_line


def main(argv: list[str]) -> int:
    # Windows 控制台默认 GBK；样例输出含中文与箭头，固定 UTF-8 避免半路崩
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="FlowRun 样例剧本")
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    arguments = parser.parse_args(argv)

    temporary_root = Path(tempfile.mkdtemp(prefix="flowrun_demo_"))
    workspace_directory = temporary_root / "workspace"
    flow_path = temporary_root / "demo.mmd"
    print(f"临时目录：{temporary_root}", flush=True)
    try:
        print("\n=== [0] 编译样例流程图 ===", flush=True)
        write_demo_flow(flow_path)
        record_check(
            "compile 返回 0", run_flowrun(["compile", str(flow_path)]) == EXIT_CODE_DONE
        )

        print("\n=== [1] 首次 run：两个任务并行 → join → 判定 → 停在人审 ===", flush=True)
        first_exit_code = run_flowrun(
            ["run", str(flow_path), "--workspace", str(workspace_directory)]
        )
        record_check("首次 run 停在等待态（exit 1）", first_exit_code == EXIT_CODE_WAITING)
        list_workspace_files(workspace_directory)

        records = read_ledger_records(workspace_directory)
        print("\n账本事件序列：\n  " + describe_ledger(records), flush=True)
        event_types = [record.get("type") for record in records]
        record_check("账本含 human_requested（流程停在人审）", "human_requested" in event_types)
        record_check(
            "两个任务节点的产物都落盘",
            all(
                (workspace_directory / product_path).is_file()
                for product_path in ("src/auth.py", "tests/test_auth.py", "src/ui/Login.jsx")
            ),
        )
        record_check(
            "审计文件按 <节点>-<种类>-<attempt> 布局落盘",
            (workspace_directory / "audit" / "T001-agent-1.stdout.log").is_file()
            and (workspace_directory / "audit" / "D001-judge-1.stdout.log").is_file(),
        )

        print("\n=== [2] 监控面：status / todo ===", flush=True)
        record_check(
            "status 能读出待人审节点（exit 0）",
            run_flowrun(
                ["status", "--workspace", str(workspace_directory), "--flow", str(flow_path)]
            )
            == EXIT_CODE_DONE,
        )
        record_check(
            "todo 重写投影成功（exit 0）",
            run_flowrun(
                ["todo", "--workspace", str(workspace_directory), "--flow", str(flow_path)]
            )
            == EXIT_CODE_DONE,
        )
        human_line = print_todo_human_line(workspace_directory)
        record_check(
            "todo 人审行可勾选且标了 awaiting_human",
            "awaiting_human" in human_line and "- [ ] H001" in human_line,
        )

        print("\n=== [3] approve + 再 run：收口 ===", flush=True)
        record_check(
            "approve H001 落账（exit 0）",
            run_flowrun(
                [
                    "approve",
                    "H001",
                    "--flow",
                    str(flow_path),
                    "--workspace",
                    str(workspace_directory),
                ]
            )
            == EXIT_CODE_DONE,
        )
        record_check(
            "再 run 收口 flow_done（exit 0）",
            run_flowrun(["run", str(flow_path), "--workspace", str(workspace_directory)])
            == EXIT_CODE_DONE,
        )
        final_records = read_ledger_records(workspace_directory)
        record_check(
            "账本以 flow_done 收尾且事件数增长",
            final_records[-1].get("type") == "flow_done" and len(final_records) > len(records),
        )
        record_check(
            "人审后的节点产物也已落盘",
            (workspace_directory / "release-tag.txt").is_file(),
        )

        print("\n=== [4] 失败路径 A：agent 自称做完但一个产物都没写 ===", flush=True)
        failing_workspace = temporary_root / "workspace_missing_outputs"
        failing_flow = write_demo_flow(
            temporary_root / "demo_missing_outputs.mmd",
            worker_extra_arguments="--mode lie",
        )
        record_check(
            "缺产物时暂停并触发 retry 闸（exit 1）",
            run_flowrun(["run", str(failing_flow), "--workspace", str(failing_workspace)])
            == EXIT_CODE_WAITING,
        )
        failing_event_types = [
            record.get("type") for record in read_ledger_records(failing_workspace)
        ]
        record_check(
            "账本记录了节点失败与闸触发",
            "node_failed" in failing_event_types and "gate_tripped" in failing_event_types,
        )
        record_check(
            "失败节点没有产物（核对确实拦下来了）",
            not (failing_workspace / "src" / "auth.py").exists(),
        )

        print("\n=== [5] 失败路径 B：判定 agent 回不存在的边 ===", flush=True)
        miss_workspace = temporary_root / "workspace_judge_miss"
        miss_flow = write_demo_flow(
            temporary_root / "demo_judge_miss.mmd", judge_extra_arguments="--mode miss"
        )
        record_check(
            "判定失败后流程暂停（exit 1）",
            run_flowrun(["run", str(miss_flow), "--workspace", str(miss_workspace)])
            == EXIT_CODE_WAITING,
        )
        miss_records = read_ledger_records(miss_workspace)
        miss_event_types = [record.get("type") for record in miss_records]
        verdict_record = next(
            (record for record in miss_records if record.get("type") == "verdict"), None
        )
        verdict_payload = (verdict_record or {}).get("payload", {})
        record_check(
            "非法边被拒（verdict.edge 为 null、tripped=true）",
            verdict_payload.get("edge") is None and verdict_payload.get("tripped") is True,
        )
        record_check("verdict 闸被触发", "gate_tripped" in miss_event_types)

        print("\n=== [6] 失败路径 C：agent 进程级失败（退出码非 0） ===", flush=True)
        crash_workspace = temporary_root / "workspace_agent_crash"
        crash_flow = write_demo_flow(
            temporary_root / "demo_agent_crash.mmd", worker_extra_arguments="--mode crash"
        )
        record_check(
            "agent 退出码非 0 时暂停（exit 1）",
            run_flowrun(["run", str(crash_flow), "--workspace", str(crash_workspace)])
            == EXIT_CODE_WAITING,
        )
        crash_failure_reasons = [
            record["payload"].get("reason")
            for record in read_ledger_records(crash_workspace)
            if record.get("type") == "node_failed"
        ]
        record_check(
            "失败原因记为 nonzero_exit",
            crash_failure_reasons and set(crash_failure_reasons) == {"nonzero_exit"},
        )

        print("\n=== 验收表 ===", flush=True)
        passed_count = sum(1 for _, passed in CHECKS if passed)
        for description, passed in CHECKS:
            print(f"  [{'OK' if passed else 'FAIL'}] {description}", flush=True)
        print(f"\n{passed_count}/{len(CHECKS)} 项通过", flush=True)
        return 0 if passed_count == len(CHECKS) else 1
    finally:
        if arguments.keep:
            print(f"\n已保留：{temporary_root}", flush=True)
        else:
            shutil.rmtree(temporary_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))