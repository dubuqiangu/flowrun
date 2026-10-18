"""FlowRun 域D2 · todo.md 投影（tasks.md T011，格式规格 design.md §4.2）。

todo.md 是「ledger 状态的只读投影 + 用户输入信号」二合一文件：

- ``write_todo``：从 FlowState 派生各节点的勾选状态，生成/覆盖 todo.md；
  全局暂停时在头部注释区插入 FLOW PAUSED 横幅，让用户一眼看到"卡在哪"；
- ``apply_user_marks``：解析用户的四类合法操作（人审行勾选 / 暂停 / 跳过 /
  恢复指令），落账为 ``human_resolved`` / ``user_directive`` / ``flow_resumed``
  事件后才生效——**不做双向同步**（design.md §4.2：双向同步是状态发散的经典
  死法）。

指令标记语法（v2，**行尾 HTML 注释形式**）：只有 ``<!-- @pause -->`` /
``<!-- @skip -->`` / ``<!-- @resume -->``（注释内外空白随意）被解析。旧版裸标记
（节点行里直接写 ``@pause``）**不再生效**——节点 label 是流程作者写的自由文本，
裸标记会把 label 文本误当用户指令（实测可把已 done 的流重新置为 paused）。
用户若仍写裸标记，本轮每文件至多给一条可读告警，提示改用注释形式（防静默失效）。

安全性：todo 是用户可自由编辑的不可信输入——只做逐行结构化解析（正则提取
节点 id / 勾选位 / 行尾标记），绝不 eval/执行；增删节点行一律忽略并向
stderr 告警（interfaces.md §7）。

幂等：FlowState 已生效的操作不重复落账——人审行节点已 done、``@pause``
节点已 paused、``@skip`` 节点已 skipped、``@resume`` 但流已非暂停则跳过。
即使发生重复落账，也会因 ledger 的 idem 去重在重放时被丢弃，但本模块以
"先判已生效、不落"为第一道防线，保持账本干净。

与 design.md §4.2 示例的两处有意差异：
1. 示例状态注释带 seq（``<!-- done @seq41 -->``），但域B 冻结的 FlowState
   投影不携带 seq，故只写状态名（``<!-- done -->``）；
2. skipped 节点保持未勾选——勾选状态只来自 node_done / human_resolved
   （design.md §4.2 明文），skipped 以状态注释标注，不用勾选框伪装完成。

用户标记的解析与应用（``apply_user_marks`` / ``append_flow_resumed_event``）已拆到
独立模块 ``kernel.todo_user_marks``（见该模块 docstring）；本模块再导出这三个名字
以保持既有 import 路径不变。
"""

from __future__ import annotations

from pathlib import Path

from flowrun.ir.schema import FlowIR, NODE_KIND_HUMAN
from flowrun.kernel.todo_user_marks import (
    TodoNodeMark,
    append_flow_resumed_event,
    apply_user_marks,
)
from flowrun.state.flow_state import (
    STATUS_AWAITING_HUMAN,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PAUSED,
    STATUS_SKIPPED,
    FlowState,
)
from flowrun.state.ledger import EventLedger

__all__ = [
    "TODO_HEADER_COMMENT",
    "TODO_MARKER_COMMENT",
    "HUMAN_HINT",
    "FLOW_PAUSED_BANNER_PREFIX",
    "RESUME_HINT_TEXT",
    "TodoNodeMark",
    "write_todo",
    "apply_user_marks",
    "append_flow_resumed_event",
]

TODO_HEADER_COMMENT = "<!-- AUTO-GENERATED from flow.mmd · 勿增删节点行 -->"
# 注意：本行本身是 HTML 注释，其文案**不得再含 "<!--"**，否则会产生嵌套注释
# （Markdown 会在第一个 "-->" 处提前闭合）。故此处只描述语法，不写示例。
TODO_MARKER_COMMENT = (
    "<!-- 用户指令写成节点行行尾的 HTML 注释：@pause（挂起此处）"
    "@skip（跳过该节点）@resume（恢复整条流） -->"
)
HUMAN_HINT = "← 此行可勾选"
FLOW_PAUSED_BANNER_PREFIX = "FLOW PAUSED"
RESUME_HINT_TEXT = "运行 flowrun resume 恢复，或在节点行行尾用 HTML 注释标记 @resume"

# 生成行尾状态注释的非缺省状态（planned/ready/running 等不加注释，保持文件干净）
_COMMENTED_STATUSES = frozenset(
    {STATUS_DONE, STATUS_SKIPPED, STATUS_FAILED, STATUS_AWAITING_HUMAN, STATUS_PAUSED}
)


def write_todo(
    flow_ir: FlowIR,
    flow_state: FlowState,
    todo_path: Path,
    ledger: EventLedger | None = None,
) -> Path:
    """从 FlowState 派生 todo.md 并整体覆盖写入，返回 todo 路径。

    勾选 = 节点状态为 done（只来自 ledger 的 node_done / human_resolved(approve)）。
    节点行顺序 = flow_ir.nodes 的声明顺序；人审节点行尾追加可勾选提示。
    全局暂停时在头部注释区插入 FLOW PAUSED 横幅——闸名/数值优先取账本最近一条
    gate_tripped payload（ledger 传入时），取不到则退化为 FlowState.tripped_gates()
    的闸名，再取不到只报 PAUSED + 恢复提示。ledger 是可选富化输入（内核调用不带）。
    """
    todo_path = Path(todo_path)
    lines = [TODO_HEADER_COMMENT]
    flow_paused_banner = _compose_flow_paused_banner(flow_state, ledger)
    if flow_paused_banner is not None:
        lines.append(flow_paused_banner)
    for node_id, node_spec in flow_ir.nodes.items():
        node_status = flow_state.node_status(node_id)
        checkbox = "x" if node_status == STATUS_DONE else " "
        line = f"- [{checkbox}] {node_id} {node_spec.label}".rstrip()
        if node_spec.kind == NODE_KIND_HUMAN:
            line = f"{line}  {HUMAN_HINT}"
        if node_status in _COMMENTED_STATUSES:
            line = f"{line}  <!-- {node_status} -->"
        lines.append(line)
    lines.append(TODO_MARKER_COMMENT)
    todo_path.parent.mkdir(parents=True, exist_ok=True)
    todo_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return todo_path


# ---------------------------------------------------------------- 内部实现


def _compose_flow_paused_banner(
    flow_state: FlowState, ledger: EventLedger | None
) -> str | None:
    """全局暂停时的头部横幅（非暂停返回 None）。"""
    if not flow_state.is_paused():
        return None
    gate_name, actual, limit = _latest_gate_finding(flow_state, ledger)
    if gate_name is not None and actual is not None and limit is not None:
        headline = (
            f"{FLOW_PAUSED_BANNER_PREFIX}: "
            f"gate={gate_name} actual={actual} limit={limit}"
        )
    elif gate_name is not None:
        headline = f"{FLOW_PAUSED_BANNER_PREFIX}: gate={gate_name}"
    else:
        headline = FLOW_PAUSED_BANNER_PREFIX
    return f"<!-- {headline} — {RESUME_HINT_TEXT} -->"


def _latest_gate_finding(
    flow_state: FlowState, ledger: EventLedger | None
) -> tuple[object, object, object]:
    """最近一次闸命中的 (闸名, actual, limit)；无闸信息时三者皆 None。

    闸名以 FlowState.tripped_gates() 为准（投影只读事实）；actual/limit 只在
    ledger 里，故按该闸名回查账本最近一条匹配的 gate_tripped payload。
    """
    tripped_gate_names = flow_state.tripped_gates()
    if not tripped_gate_names:
        return None, None, None
    latest_gate_name = tripped_gate_names[-1]
    if ledger is not None:
        for entry in reversed(ledger.entries()):
            if (
                entry.type == "gate_tripped"
                and entry.payload.get("gate") == latest_gate_name
            ):
                return (
                    latest_gate_name,
                    entry.payload.get("actual"),
                    entry.payload.get("limit"),
                )
    return latest_gate_name, None, None
