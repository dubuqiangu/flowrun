"""FlowRun CLI（域D3 装配入口，interfaces.md §0：入口只做装配）。

argparse 子命令 → 各域模块调用的薄装配层，不承载业务逻辑：

- ``compile <flow.mmd>``：编译校验，好图打印 FlowIR JSON；坏图打印行号错误清单；
- ``run <flow.mmd> --workspace <dir> [--dispatch-timeout 秒]``：编译 → **校验
  工作区身份（先于任何写入；不符退 4 且不污染工作区）** → 应用 todo 用户标记 →
  生成 AGENTS.stub → run_flow；
- ``resume --flow <mmd> --workspace <dir>``：解除闸/暂停挂起——恢复 FlowState →
  校验工作区与图身份 → 落 ``flow_resumed``（唯一 cli 恢复通道）；
- ``approve|deny <node> --flow <mmd> --workspace <dir>``：人审裁决落
  ``human_resolved``（幂等：FlowState 已解决则跳过）；
- ``status --workspace <dir> [--flow <mmd>]``：从 ledger+快照恢复 FlowState，
  打印节点状态、token/hop 与三闸阈值、待人审节点（只读）；
- ``todo --workspace <dir> --flow <mmd>``：先应用 todo 用户标记，再重写投影。

退出码契约（脚本可区分失败类别）：
0=done / 1=waiting(paused/awaiting_human) / 2=halted /
3=编译·校验·IO 失败（读流程文件 / 写工作区盘）/ 4=工作区与流程身份不匹配 /
5=工作区未初始化 / 6=账本损坏（中段不可解析或哈希链断裂，需人工检查或换新工作区）。

2=halted（本轮定稿）：halted 是 flow_state 的终止态，M1 尚无其生产者（`flow_halted`
随 T019 落地），故**不新增 `halt` 子命令**；但账本一旦已含 `flow_halted`，`run`
（收口）/ `resume`（拒绝恢复）/ `status`（只读检出）三通道都以 2 上报，使其成为
可被测试钉住的契约（tests/test_cli_halted_terminal.py）。

异步入口统一 ``asyncio.run``（仅标准库）。所有对外输入只进结构化解析。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from flowrun.compiler.frontmatter import FrontmatterError
from flowrun.compiler.mermaid_parser import MermaidParseError
from flowrun.compiler.validator import FlowValidationError, compile_flow_source
from flowrun.ir.schema import FlowIR
from flowrun.kernel.engine import (
    FlowWorkspaceMismatchError,
    run_flow,
    verify_workspace_matches_flow,
)
from flowrun.kernel.hooks_assembly import (
    DEFAULT_DISPATCH_TIMEOUT_SECONDS,
    build_engine_hooks,
)
from flowrun.kernel.state_summary import print_state_summary
from flowrun.kernel.stub_generator import generate_agents_stub
from flowrun.kernel.todo_projection import (
    append_flow_resumed_event,
    apply_user_marks,
    write_todo,
)
from flowrun.kernel.workspace_session import (
    append_human_resolution,
    open_workspace_state,
    workspace_is_initialized,
    workspace_ledger_path,
)
from flowrun.kernel.workspace_layout import resolve_layout_for_read
from flowrun.state.flow_state import (
    STATUS_AWAITING_HUMAN,
    STATUS_DONE,
    STATUS_FAILED,
)
from flowrun.state.ledger import EventLedger, LedgerCorruptionError
from flowrun.state.snapshots import SnapshotStore, recover_flow_state

__all__ = ["main"]

EXIT_FLOW_DONE = 0
EXIT_WAITING = 1
EXIT_FLOW_HALTED = 2
EXIT_COMPILE_ERROR = 3
EXIT_WORKSPACE_MISMATCH = 4
EXIT_WORKSPACE_MISSING = 5
EXIT_LEDGER_CORRUPTION = 6


class FlowFileReadError(Exception):
    """读取流程文件（flow.mmd）失败——单独成类，使 main 能把"读流程文件失败"
    与"工作区写盘失败"两类 OSError 分开报（改前统一误报成"无法读取流程文件"）。

    与 OSError 无继承关系（直接继承 Exception），故 main 的 except 子句顺序无关。
    """


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：解析参数并执行子命令，返回进程退出码。"""
    parser = build_argument_parser()
    parsed_args = parser.parse_args(argv)
    try:
        return parsed_args.handler(parsed_args)
    except FlowWorkspaceMismatchError as mismatch_error:
        print(f"flowrun: 工作区与流程身份不匹配 - {mismatch_error}", file=sys.stderr)
        return EXIT_WORKSPACE_MISMATCH
    except FlowValidationError as validation_error:
        for validation_finding in validation_error.errors:
            print(f"flow.mmd:{validation_finding}", file=sys.stderr)
        return EXIT_COMPILE_ERROR
    except (MermaidParseError, FrontmatterError) as parse_error:
        print(f"flow.mmd:{parse_error}", file=sys.stderr)
        return EXIT_COMPILE_ERROR
    # 三个子句分属互无继承关系的异常树：LedgerCorruptionError 与 FlowFileReadError
    # 都直接继承 Exception（不是 OSError 子类），故不会被 except OSError 抢先捕获，
    # 先后顺序不影响正确性；把它们排在 OSError 之前只为"更具体的业务语义"优先。
    except LedgerCorruptionError as ledger_corruption_error:
        print(
            f"flowrun: {_describe_ledger_corruption(ledger_corruption_error, parsed_args)}",
            file=sys.stderr,
        )
        return EXIT_LEDGER_CORRUPTION
    except FlowFileReadError as flow_file_read_error:
        print(f"flowrun: 无法读取流程文件 - {flow_file_read_error}", file=sys.stderr)
        return EXIT_COMPILE_ERROR
    except OSError as workspace_write_error:
        # 读流程文件已在上方收敛为 FlowFileReadError，故此处剩余的 OSError 都来自
        # 工作区写入路径（账本 / todo / packet / AGENTS.stub 等）——按此措辞诊断，
        # 不再误报成"无法读取流程文件"。退出码沿用 3（既有语义表：编译·校验·IO 失败）。
        print(
            f"flowrun: 工作区写盘失败（账本 / todo / 产物等无法写入）- "
            f"{workspace_write_error}",
            file=sys.stderr,
        )
        return EXIT_COMPILE_ERROR


def _describe_ledger_corruption(
    ledger_corruption_error: LedgerCorruptionError, parsed_args: argparse.Namespace
) -> str:
    """账本损坏的可读诊断（含工作区账本路径与原因，不给用户看 traceback）。

    异常自身的文案只在"行级解析失败"时带路径，链断裂/快照核对失败不带；故此处
    从子命令参数补出账本路径，保证任何损坏分支都指得到该检查哪个文件。
    """
    workspace_text = getattr(parsed_args, "workspace", None)
    ledger_path_hint = (
        f"账本 {workspace_ledger_path(Path(workspace_text))}"
        if workspace_text
        else "工作区账本"
    )
    return (
        f"账本损坏，无法恢复流程状态——请检查 {ledger_path_hint} 或改用新工作区；"
        f"原因：{ledger_corruption_error}"
    )


def build_argument_parser() -> argparse.ArgumentParser:
    """装配 argparse 子命令树（handler 由 set_defaults 注入）。"""
    parser = argparse.ArgumentParser(
        prog="flowrun", description="Mermaid 流程图驱动的多 Agent 编排运行时（M1）"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    compile_parser = subparsers.add_parser(
        "compile", help="编译校验 flow.mmd，打印 FlowIR JSON"
    )
    compile_parser.add_argument("flow_path", help="flow.mmd 文件路径")
    compile_parser.set_defaults(handler=_handle_compile)

    run_parser = subparsers.add_parser("run", help="驱动流程到等待点或完成")
    run_parser.add_argument("flow_path", help="flow.mmd 文件路径")
    run_parser.add_argument("--workspace", required=True, help="工作区目录")
    run_parser.add_argument(
        "--dispatch-timeout",
        type=float,
        default=DEFAULT_DISPATCH_TIMEOUT_SECONDS,
        help=f"任务投递超时秒数（默认 {DEFAULT_DISPATCH_TIMEOUT_SECONDS:g}）",
    )
    run_parser.set_defaults(handler=_handle_run)

    resume_parser = subparsers.add_parser(
        "resume", help="解除闸/暂停挂起，落 flow_resumed 后可用 run 续跑"
    )
    resume_parser.add_argument("--flow", required=True, help="flow.mmd 文件路径")
    resume_parser.add_argument("--workspace", required=True, help="工作区目录")
    resume_parser.set_defaults(handler=_handle_resume)

    approve_parser = subparsers.add_parser("approve", help="人审节点裁决：通过")
    _add_resolution_arguments(approve_parser)
    approve_parser.set_defaults(handler=_handle_approve)

    deny_parser = subparsers.add_parser("deny", help="人审节点裁决：否决")
    _add_resolution_arguments(deny_parser)
    deny_parser.set_defaults(handler=_handle_deny)

    status_parser = subparsers.add_parser("status", help="恢复并打印流程状态")
    status_parser.add_argument("--workspace", required=True, help="工作区目录")
    status_parser.add_argument(
        "--flow", default=None, help="flow.mmd 路径（提供后额外显示预算阈值）"
    )
    status_parser.set_defaults(handler=_handle_status)

    todo_parser = subparsers.add_parser(
        "todo", help="应用 todo 用户标记并重写 todo 投影"
    )
    todo_parser.add_argument("--workspace", required=True, help="工作区目录")
    todo_parser.add_argument("--flow", required=True, help="flow.mmd 文件路径")
    todo_parser.set_defaults(handler=_handle_todo)

    return parser


def _add_resolution_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("node_id", help="人审节点 ID")
    parser.add_argument("--flow", required=True, help="flow.mmd 文件路径")
    parser.add_argument("--workspace", required=True, help="工作区目录")


# ---------------------------------------------------------------- 子命令


def _handle_compile(parsed_args: argparse.Namespace) -> int:
    flow_ir = _compile_flow_file(Path(parsed_args.flow_path))
    print(json.dumps(flow_ir.to_dict(), ensure_ascii=False, indent=2))
    return EXIT_FLOW_DONE


def _handle_run(parsed_args: argparse.Namespace) -> int:
    flow_ir = _compile_flow_file(Path(parsed_args.flow_path))
    workspace = Path(parsed_args.workspace).resolve()

    # 身份校验必须先于一切写入：拿错工作区（B 流程跑 A 工作区）时不得污染它——
    # 不落用户标记、不生成 AGENTS.stub.md、不建 packets/audit/snapshots。
    # 校验逻辑唯一真相在 kernel.engine.verify_workspace_matches_flow（零写入纯查询）；
    # 空账本 = 全新工作区，天然通过，故 run 仍可直接冷启动新工作区（不退 5）。
    #
    # 账本单实例：本次 run 全程只开这一个 EventLedger，依次喂 build_engine_hooks /
    # run_flow（借用，绝不由内核 close）/ write_todo / print_state_summary，
    # 最后一次 close（进程内不再出现第二个指向 ledger.jsonl 的写者）。
    layout = resolve_layout_for_read(workspace, flow_ir.run_name)
    ledger, flow_state = open_workspace_state(workspace, flow_ir.run_name)
    try:
        verify_workspace_matches_flow(flow_ir, ledger)
        layout.run_root.mkdir(parents=True, exist_ok=True)
        # 上轮等待点留在 todo.md 的勾选/@pause/@skip/@resume 先落账，再驱动内核
        apply_user_marks(flow_ir, layout.todo_path, ledger, flow_state)

        # AGENTS.stub.md 属工作区级资产：写工作区根，各 run 共享同一份。
        generate_agents_stub(flow_ir, layout.workspace_root)
        engine_hooks = build_engine_hooks(
            flow_ir, layout.run_root, ledger, parsed_args.dispatch_timeout
        )
        final_state = asyncio.run(
            run_flow(flow_ir, layout.run_root, engine_hooks, ledger)
        )
        # 内核收口时写的 todo 拿不到闸 payload：用同一账本重写一次补 PAUSED 头注释；
        # 同一句柄顺带喂给摘要读 node_failed payload（T020j），读完即关。
        write_todo(flow_ir, final_state, layout.todo_path, ledger)
        print_state_summary(
            flow_ir,
            final_state,
            title=f"flowrun run: {flow_ir.run_name}",
            ledger=ledger,
        )
    finally:
        ledger.close()
    if final_state.is_done():
        return EXIT_FLOW_DONE
    if final_state.is_halted():
        return EXIT_FLOW_HALTED
    return EXIT_WAITING


def _handle_resume(parsed_args: argparse.Namespace) -> int:
    """解除闸/暂停挂起：身份校验 → 状态校验 → 落 flow_resumed（唯一 cli 通道）。"""
    flow_ir = _compile_flow_file(Path(parsed_args.flow))
    workspace = Path(parsed_args.workspace)
    if not workspace_is_initialized(workspace, flow_ir.run_name):
        print("flowrun: 工作区未初始化（缺少 ledger.jsonl）", file=sys.stderr)
        return EXIT_WORKSPACE_MISSING
    ledger, flow_state = open_workspace_state(workspace, flow_ir.run_name)
    try:
        # 身份校验唯一真相在 kernel.engine.verify_workspace_matches_flow（零写入纯查询，
        # 可直接在已打开的账本上调用），与 run 同口径：不仅比 run 名，同 run 名而节点集
        # 被替换也拒绝。不符时由 main 统一捕获 FlowWorkspaceMismatchError（退 4）。
        verify_workspace_matches_flow(flow_ir, ledger)
        if flow_state.is_halted():
            # halted 是终止态（并非"稍后可 resume 的挂起"）：拒绝恢复、按退出码表
            # 以 2 上报，且不落任何事件（halted 不被 resume 覆盖，见 flow_state 投影）。
            print("flowrun: 流已终止（halted），拒绝恢复", file=sys.stderr)
            return EXIT_FLOW_HALTED
        if flow_state.is_done():
            print("flowrun: 流已完成（done），拒绝恢复", file=sys.stderr)
            return EXIT_WAITING
        if not flow_state.is_paused():
            print(
                "flowrun: 流未处于暂停状态，无需恢复"
                "（仅闸触发或 @pause 挂起后可用 resume）",
                file=sys.stderr,
            )
            return EXIT_WAITING
        append_flow_resumed_event(flow_ir, ledger, by="cli", flow_state=flow_state)
    finally:
        ledger.close()
    print(f"已恢复：{flow_ir.run_name}")
    return EXIT_FLOW_DONE


def _handle_approve(parsed_args: argparse.Namespace) -> int:
    return _resolve_human_node(parsed_args, decision="approve")


def _handle_deny(parsed_args: argparse.Namespace) -> int:
    return _resolve_human_node(parsed_args, decision="deny")


def _handle_status(parsed_args: argparse.Namespace) -> int:
    workspace = Path(parsed_args.workspace)
    # status 不强制 --flow，故 run 名可能未知：只给了 run 名(经 --flow 编译)时按
    # 该 run 的账本读；未给时按工作区根读（legacy / 单 run 工作区的既有语义）。
    flow_ir = _compile_flow_file(Path(parsed_args.flow)) if parsed_args.flow else None
    run_name = flow_ir.run_name if flow_ir is not None else None
    layout = resolve_layout_for_read(workspace, run_name)
    if not layout.ledger_path.exists():
        print("flowrun: 工作区未初始化（缺少 ledger.jsonl）", file=sys.stderr)
        return EXIT_WORKSPACE_MISSING
    ledger, flow_state = open_workspace_state(workspace, run_name)
    try:
        print_state_summary(
            flow_ir, flow_state, title=f"flowrun status: {workspace}", ledger=ledger
        )
        # status 只读检出：查询成功即 0，唯独 halted 以 2 上报（与 run/resume 同口径）。
        exit_code = EXIT_FLOW_HALTED if flow_state.is_halted() else EXIT_FLOW_DONE
    finally:
        ledger.close()
    return exit_code


def _handle_todo(parsed_args: argparse.Namespace) -> int:
    flow_ir = _compile_flow_file(Path(parsed_args.flow))
    workspace = Path(parsed_args.workspace)
    if not workspace_is_initialized(workspace, flow_ir.run_name):
        print("flowrun: 工作区未初始化（缺少 ledger.jsonl）", file=sys.stderr)
        return EXIT_WORKSPACE_MISSING
    layout = resolve_layout_for_read(workspace, flow_ir.run_name)
    ledger, flow_state = open_workspace_state(workspace, flow_ir.run_name)
    try:
        applied_event_count = apply_user_marks(
            flow_ir, layout.todo_path, ledger, flow_state
        )
        write_todo(flow_ir, flow_state, layout.todo_path, ledger)
    finally:
        ledger.close()
    print(f"todo 投影已重写，应用了 {applied_event_count} 条用户标记")
    return EXIT_FLOW_DONE


# ---------------------------------------------------------------- 装配工具


def _compile_flow_file(flow_path: Path) -> FlowIR:
    """读文件并走完整编译管线（坏图由 main 统一捕获打印）。

    读取失败单独收敛为 FlowFileReadError，使 main 能把"读流程文件失败"与"工作区
    写盘失败"分开诊断（改前两者都落进同一个 except OSError，统一误报成读取失败）。
    """
    flow_path = Path(flow_path)
    try:
        flow_source = flow_path.read_text(encoding="utf-8")
    except OSError as flow_read_error:
        raise FlowFileReadError(f"{flow_path}：{flow_read_error}") from flow_read_error
    return compile_flow_source(flow_source)


def _resolve_human_node(parsed_args: argparse.Namespace, decision: str) -> int:
    """approve/deny 共用装配：幂等校验 → 落 human_resolved → 重写 todo 投影。"""
    flow_ir = _compile_flow_file(Path(parsed_args.flow))
    node_id = parsed_args.node_id
    if node_id not in flow_ir.nodes:
        print(f"flowrun: 流程图中不存在节点 {node_id}", file=sys.stderr)
        return EXIT_WAITING
    workspace = Path(parsed_args.workspace)
    layout = resolve_layout_for_read(workspace, flow_ir.run_name)
    ledger, flow_state = open_workspace_state(workspace, flow_ir.run_name)
    try:
        node_status = flow_state.node_status(node_id)
        if decision == "approve" and node_status == STATUS_DONE:
            print(f"{node_id} 已通过（done），跳过重复 approve")
            return EXIT_FLOW_DONE
        if decision == "deny" and node_status == STATUS_FAILED:
            print(f"{node_id} 已是失败态（failed），跳过重复 deny")
            return EXIT_FLOW_DONE
        if node_status != STATUS_AWAITING_HUMAN:
            print(
                f"flowrun: 节点 {node_id} 当前状态为 {node_status}，"
                "不在人审挂起（awaiting_human），拒绝裁决",
                file=sys.stderr,
            )
            return EXIT_WAITING
        append_human_resolution(flow_ir, ledger, node_id, decision)
        updated_state = recover_flow_state(ledger, SnapshotStore(layout.snapshots_dir))
        write_todo(flow_ir, updated_state, layout.todo_path, ledger)
    finally:
        ledger.close()
    decision_text = "通过" if decision == "approve" else "否决"
    print(f"{node_id} 人审裁决已落账：{decision_text}")
    return EXIT_FLOW_DONE


if __name__ == "__main__":
    sys.exit(main())
