"""FlowRun 域D2 · todo.md 投影（tasks.md T011，格式规格 design.md §4.2）。

todo.md 是「ledger 状态的只读投影 + 用户输入信号」二合一文件：

- ``write_todo``：从 FlowState 派生各节点的勾选状态，生成/覆盖 todo.md；
  全局暂停时在头部注释区插入 FLOW PAUSED 横幅，让用户一眼看到"卡在哪"；
- ``apply_user_marks``：解析用户的四类合法操作（人审行勾选 / 暂停 / 跳过 /
  恢复指令），落账为 ``human_resolved`` / ``user_directive`` / ``flow_resumed``
  事件后才生效——**不做双向同步**（design.md §4.2：双向同步是
  状态发散的经典死法）。

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
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from flowrun.ir.schema import FlowIR, NODE_KIND_HUMAN
from flowrun.state.flow_state import (
    STATUS_AWAITING_HUMAN,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PAUSED,
    STATUS_SKIPPED,
    FlowState,
)
from flowrun.state.ledger import EventLedger, LedgerEntry

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

_CHECKBOX_LINE_PATTERN = re.compile(r"^\s*-\s*\[([ xX])\]\s+(\S+)(.*)$")
# 指令只认行尾 HTML 注释形式；裸 @pause/@skip/@resume 一律不解析（见模块 docstring）
_PAUSE_MARKER_PATTERN = re.compile(r"<!--\s*@pause\s*-->")
_SKIP_MARKER_PATTERN = re.compile(r"<!--\s*@skip\s*-->")
_RESUME_MARKER_PATTERN = re.compile(r"<!--\s*@resume\s*-->")
_HTML_COMMENT_PATTERN = re.compile(r"<!--.*?-->")
_LEGACY_BARE_MARKER_PATTERN = re.compile(r"@(?:pause|skip|resume)\b")
# user_directive skip 不再作用于终态节点（与 FlowState 投影规则一致，双保险）
_NOT_SKIPPABLE_STATUSES = frozenset({STATUS_DONE, STATUS_FAILED, STATUS_SKIPPED})


@dataclass
class TodoNodeMark:
    """todo.md 中一行节点标记的结构化解析结果（只含结构化字段，无原始文本）。"""

    node_id: str
    checked: bool
    pause_directive: bool
    skip_directive: bool
    resume_directive: bool


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


def apply_user_marks(
    flow_ir: FlowIR,
    todo_path: Path,
    ledger: EventLedger,
    flow_state: FlowState,
) -> int:
    """解析 todo.md 中的用户标记并落账生效，返回本次新落账的事件数。

    四类合法操作（design.md §4.2 + M1 恢复通道）：
    - 人审节点行勾选 ``[x]`` → ``human_resolved{"decision": "approve"}``；
    - 行尾 ``<!-- @pause -->`` → ``user_directive{"directive": "pause"}``；
    - 行尾 ``<!-- @skip -->`` → ``user_directive{"directive": "skip"}``；
    - 行尾 ``<!-- @resume -->``（全流落一次）→ ``flow_resumed{"by": "todo"}``。

    节点 label 里的 ``@pause`` 等**裸文本永不触发**任何落账（v2 起只认注释形式）。

    幂等：FlowState 已生效（节点已 done / paused / skipped、流已非暂停）的操作不落账，
    也不重复告警——派生勾选行（``[x]`` + ``<!-- done -->``）重跑时静默跳过。每落一条
    事件都同步投影进传入的 flow_state，调用方无需重放即可看到最新状态。todo 文件
    缺失时静默返回 0（无可应用的输入）。

    人审勾选门槛：只有状态为 ``awaiting_human`` 的人审节点，勾选才落 human_resolved；
    未到达即勾选一律拒绝（不落账、不判 done），并按降噪政策每轮每文件至多一条同类告警。
    """
    todo_path = Path(todo_path)
    try:
        todo_text = todo_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    marks, marked_node_ids = _parse_marked_nodes(todo_text)
    applied_event_count = 0
    # 本轮每文件已发过的告警类别：同类告警只发一条（降噪政策，参 T020c）。
    warned_categories: set[str] = set()
    for mark in marks:
        node_spec = flow_ir.nodes.get(mark.node_id)
        if node_spec is None:
            _warn(f"todo 行引用了流程图之外的节点 {mark.node_id}，忽略（勿增删节点行）")
            continue
        applied_event_count += _apply_single_mark(
            flow_ir, node_spec, mark, ledger, flow_state, warned_categories
        )
    if any(mark.resume_directive for mark in marks):
        applied_event_count += _apply_resume_request(flow_ir, ledger, flow_state)
    for node_id in flow_ir.nodes:
        if node_id not in marked_node_ids:
            _warn(f"todo 缺少节点 {node_id} 的行（节点行被删？），本次忽略该节点")
    return applied_event_count


def append_flow_resumed_event(
    flow_ir: FlowIR,
    ledger: EventLedger,
    by: str,
    flow_state: FlowState | None = None,
) -> LedgerEntry:
    """落一条 flow_resumed（CLI ``resume`` 与 todo ``@resume`` 共用的唯一生产者）。

    格式冻结：payload = ``{"by": "cli"}`` 或 ``{"by": "todo"}``；
    idem = ``{run_name}-flow-resumed-{序号}``，序号 = 账内既有 flow_resumed 条数 + 1。
    flow_state 非空时同步投影该事件，调用方无需重放即可看到最新状态。
    """
    prior_resume_count = sum(
        1 for entry in ledger.entries() if entry.type == "flow_resumed"
    )
    appended_entry = ledger.append(
        "flow_resumed",
        {"by": by},
        f"{flow_ir.run_name}-flow-resumed-{prior_resume_count + 1}",
        flow_ir.run_name,
    )
    if flow_state is not None:
        flow_state.apply_event(appended_entry)
    return appended_entry


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


def _parse_marked_nodes(todo_text: str) -> tuple[list[TodoNodeMark], set[str]]:
    """逐行解析勾选框行 → (标记列表, 出现过的节点 id 集合)。

    非勾选框行（头尾注释等）跳过不告警——用户被允许在节点行之外写自由文本。
    解析完再对整份文本做一次旧式裸标记体检（告警与落账互不干扰）。
    """
    marks: list[TodoNodeMark] = []
    marked_node_ids: set[str] = set()
    for raw_line in todo_text.splitlines():
        matched = _CHECKBOX_LINE_PATTERN.match(raw_line)
        if matched is None:
            continue
        checkbox_text, node_id, trailing_text = matched.groups()
        marks.append(
            TodoNodeMark(
                node_id=node_id,
                checked=checkbox_text in "xX",
                pause_directive=_PAUSE_MARKER_PATTERN.search(trailing_text) is not None,
                skip_directive=_SKIP_MARKER_PATTERN.search(trailing_text) is not None,
                resume_directive=_RESUME_MARKER_PATTERN.search(trailing_text) is not None,
            )
        )
        marked_node_ids.add(node_id)
    _warn_on_legacy_bare_markers(todo_text)
    return marks, marked_node_ids


def _warn_on_legacy_bare_markers(todo_text: str) -> None:
    """旧式裸标记（未包进 HTML 注释）在 v2 起失效——每轮每文件至多提醒一次。

    只看节点行（非节点行是用户自由文本，且头部说明本身含裸词）；把行内的 HTML
    注释整段剔除后再找裸标记，故注释形式的合法指令不会误报。
    """
    for raw_line in todo_text.splitlines():
        if _CHECKBOX_LINE_PATTERN.match(raw_line) is None:
            continue
        line_without_comments = _HTML_COMMENT_PATTERN.sub("", raw_line)
        if _LEGACY_BARE_MARKER_PATTERN.search(line_without_comments) is not None:
            _warn(
                "节点行出现旧式裸标记（@pause/@skip/@resume 未包进 HTML 注释），"
                "v2 起已不再生效——请改写为行尾注释形式（见文件头部说明）"
            )
            return


def _apply_single_mark(
    flow_ir: FlowIR,
    node_spec,  # NodeSpec（避免循环标注依赖，运行期只读属性）
    mark: TodoNodeMark,
    ledger: EventLedger,
    flow_state: FlowState,
    warned_categories: set[str],
) -> int:
    """对单个节点应用其 todo 节点级标记（勾选 / @pause / @skip），返回新落账事件数。

    ``@resume`` 是全局标记，不在此处理（见 apply_user_marks → _apply_resume_request）。
    """
    applied_event_count = 0
    if mark.pause_directive:
        applied_event_count += _append_directive_event(
            flow_ir, node_spec, "pause", ledger, flow_state
        )
    if mark.skip_directive:
        applied_event_count += _append_directive_event(
            flow_ir, node_spec, "skip", ledger, flow_state
        )
    if mark.checked:
        if node_spec.kind != NODE_KIND_HUMAN:
            # 派生勾选行（节点已 done 时 write_todo 写的填法）不是用户误操作：
            # 静默跳过——否则每次重跑都会对同一批已 done 的 [x] 行重复刷屏告警。
            if flow_state.node_status(node_spec.node_id) != STATUS_DONE:
                _warn(
                    f"节点 {node_spec.node_id} 不是人审节点，勾选被忽略"
                    "（勾选仅对人审节点行生效）"
                )
        else:
            applied_event_count += _append_human_resolution(
                flow_ir, node_spec, ledger, flow_state, warned_categories
            )
    return applied_event_count


def _apply_resume_request(
    flow_ir: FlowIR, ledger: EventLedger, flow_state: FlowState
) -> int:
    """todo 的 @resume：仅在流确实处于暂停态时落 flow_resumed，否则告警忽略。

    幂等第一道防线：已恢复（非暂停）/ 已终止 / 已完成的状态一律不落账。
    """
    if flow_state.is_halted() or flow_state.is_done():
        _warn("todo 中的 @resume 未生效：流已终止或已完成")
        return 0
    if not flow_state.is_paused():
        _warn("todo 中的 @resume 未生效：流未处于暂停状态")
        return 0
    append_flow_resumed_event(flow_ir, ledger, by="todo", flow_state=flow_state)
    return 1


def _append_directive_event(
    flow_ir: FlowIR,
    node_spec,
    directive: str,
    ledger: EventLedger,
    flow_state: FlowState,
) -> int:
    """落账一条 user_directive 事件；已生效（幂等）则返回 0 不落。"""
    node_id = node_spec.node_id
    node_status = flow_state.node_status(node_id)
    if directive == "pause" and node_status == STATUS_PAUSED:
        return 0
    if directive == "skip" and node_status in _NOT_SKIPPABLE_STATUSES:
        return 0
    occurrence = _count_directive_events(ledger, node_id, directive)
    appended_entry = ledger.append(
        "user_directive",
        {"directive": directive},
        f"{flow_ir.run_name}-{node_id}-user-directive-{directive}-{occurrence + 1}",
        flow_ir.run_name,
        node_id,
    )
    flow_state.apply_event(appended_entry)
    return 1


def _append_human_resolution(
    flow_ir: FlowIR,
    node_spec,
    ledger: EventLedger,
    flow_state: FlowState,
    warned_categories: set[str],
) -> int:
    """人审节点勾选 → human_resolved(approve)，**仅当节点确实处于 awaiting_human**。

    改前只要节点不是 done 就落 human_resolved——勾选一个尚未走到的人审节点会立即
    把它判 done（伪造人审通过）。现况守卫：
    - 节点已 done：幂等跳过（派生 [x] 行重跑静默，无告警）；
    - 节点非 awaiting_human：拒绝落账、不产生 human_resolved 伪事件，并按降噪政策
      每轮每文件至多一条同类告警（不逐行刷屏）。
    """
    node_id = node_spec.node_id
    node_status = flow_state.node_status(node_id)
    if node_status == STATUS_DONE:
        return 0
    if node_status != STATUS_AWAITING_HUMAN:
        _warn_category_once(
            warned_categories,
            "human-check-before-review",
            f"节点 {node_id} 尚未到达人审挂起点（当前 {node_status}），勾选被忽略——"
            "只有状态为 awaiting_human 的人审节点，勾选才生效",
        )
        return 0
    resolution_count = _count_ledger_events(ledger, "human_resolved", node_id)
    appended_entry = ledger.append(
        "human_resolved",
        {"decision": "approve"},
        f"{flow_ir.run_name}-{node_id}-human-resolved-{resolution_count + 1}",
        flow_ir.run_name,
        node_id,
    )
    flow_state.apply_event(appended_entry)
    return 1


def _count_ledger_events(ledger: EventLedger, event_type: str, node_id: str) -> int:
    """账本中某节点某类型事件的出现次数（幂等键 occurrence 段的依据）。"""
    return sum(
        1
        for entry in ledger.entries()
        if entry.type == event_type and entry.node == node_id
    )


def _count_directive_events(
    ledger: EventLedger, node_id: str, directive: str
) -> int:
    """某节点同种 directive 的既有事件数（pause/resume 后的重复指令各自计一次）。"""
    return sum(
        1
        for entry in ledger.entries()
        if entry.type == "user_directive"
        and entry.node == node_id
        and entry.payload.get("directive") == directive
    )


def _warn(message: str) -> None:
    """增删节点行等用户误操作：忽略但向 stderr 告警（design.md §4.2）。"""
    print(f"[todo_projection] 告警: {message}", file=sys.stderr)


def _warn_category_once(
    warned_categories: set[str], warning_category: str, message: str
) -> None:
    """同类告警每轮每文件至多一条（降噪政策，参 T020c）：本轮该类别已发过则静默。"""
    if warning_category in warned_categories:
        return
    warned_categories.add(warning_category)
    _warn(message)
