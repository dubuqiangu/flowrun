"""FlowRun 域D2 · 工作区身份校验（FIX-B3 / K2；T022 起按 run 隔离语义收敛）。

从 ``kernel.engine`` 原样拆出（纯搬移，函数名/参数顺序/异常类型零变化），
原因：T022 加入多 run 布局后 ``engine.py`` 超过 400 行上限。公共名在
``kernel.engine`` 原路径继续导出，既有 import 不受影响。

职责单一：只回答"这份账本能不能承载当前这张图"，全部是**零写入纯查询**。
"""
from __future__ import annotations

from flowrun.ir.schema import FlowIR
from flowrun.state.ledger import EventLedger

__all__ = [
    "FlowWorkspaceMismatchError",
    "verify_workspace_matches_flow",
]


class FlowWorkspaceMismatchError(Exception):
    """工作区账本的身份（run 名 / 计划节点集合）与当前流程图不符。

    工作区是流程身份的载体：同一份账本只服务同一个 run。不符意味着用户
    拿错了账本（如 ``flowrun run b.mmd --workspace a的工作区``），此时若
    继续推进，恢复出的 A 状态会被当成 B 的状态——A 已 done 时 B 整图会被
    静默跳过。故一律拒绝，由调用方（CLI）转成退出码 4。

    T022 起"多 run 共存"由**布局**（每个 run 各自一份账本）承载，而不是让
    一份账本装载多个 run——因此本校验的语义不变：它守护的是"这一份账本
    内的 run 身份"。
    """


def verify_workspace_matches_flow(flow_ir: FlowIR, ledger: EventLedger) -> None:
    """工作区身份校验（FIX-B3 / K2，冻结公开接口）：不符抛 ``FlowWorkspaceMismatchError``。

    冻结接口（cli.py 依赖）：函数名、参数顺序、抛出的异常类型都不再变更。

    零写入纯查询：只读账本，不落事件、不建目录、不改文件；可直接在**已打开的**
    ``EventLedger`` 上调用。

    校验规则（用户裁定：**严格集合相等**，fail-closed）：
    - **空账本 = 全新工作区**，放行（首次 run 随后落 flow_started）；
    - 账本非空但**没有 flow_started** → 拒绝：这不是内核生成的合法工作区，
      身份无从校验——否则一个手工写入/陈旧账本可以绕过身份校验，把**另一张图**
      续跑上去（fail-open 漏洞）；
    - flow_started 的 **planned_nodes 缺失或为空** → 拒绝：无计划节点集可比，
      身份无从校验——否则同名的另一张图即可绕过；
    - planned_nodes 与当前图节点集必须**双向一致**：多出一个节点、或少一个
      节点都拒绝（"换图请换工作区"）。

    T022 说明：多 run 共存时各 run 各有自己的账本，因此"同工作区不同 run"的
    正常用法不再经过本函数的拒绝分支——不是放宽，而是把冲突从"共享账本里的
    run 名不等"变成"路径根本不同"。
    """
    if ledger.last_seq == 0:
        return  # 空账本 = 全新工作区：无身份可校验，放行首次 run
    flow_started_entry = next(
        (entry for entry in ledger.entries() if entry.type == "flow_started"), None
    )
    if flow_started_entry is None:
        raise FlowWorkspaceMismatchError(
            "账本非空但缺少 flow_started 事件，无法校验工作区身份"
            "（这不是内核生成的合法工作区）；请换新工作区"
        )
    recorded_run_name = flow_started_entry.run
    if recorded_run_name != flow_ir.run_name:
        raise FlowWorkspaceMismatchError(
            f"账本属于 run '{recorded_run_name}'，与 '{flow_ir.run_name}' 不匹配"
            "（同一账本不可跨流程复用：换图请换工作区或用新的 run 名）"
        )
    recorded_planned_nodes = set(flow_started_entry.payload.get("planned_nodes") or [])
    if not recorded_planned_nodes:
        raise FlowWorkspaceMismatchError(
            "账本 flow_started 缺少 planned_nodes（计划节点集为空），无法校验工作区身份；"
            "请换新工作区"
        )
    current_node_ids = set(flow_ir.nodes)
    replaced_node_ids = recorded_planned_nodes - current_node_ids
    if replaced_node_ids:
        raise FlowWorkspaceMismatchError(
            f"工作区已有节点 {sorted(replaced_node_ids)} 不在当前图中——图已被替换"
            "（同一账本不可跨流程复用：换图请换工作区或用新的 run 名）"
        )
    appended_node_ids = current_node_ids - recorded_planned_nodes
    if appended_node_ids:
        raise FlowWorkspaceMismatchError(
            f"当前图新增节点 {sorted(appended_node_ids)} 不在该工作区计划中"
            "——追加节点请换工作区或用新的 run 名"
        )