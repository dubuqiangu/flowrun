"""AGENTS.stub.md 生成器（tasks.md T012，design.md §4 用户产物规格）。

内核为工作区生成的 agent 引导桩：新会话的 agent 只读该文件即能正确理解
packet 协议、自报格式与产物契约核对规则——不需要读内核源码或流程图全貌。

桩是纯文本拼装（无模板引擎），内容为通用协议说明 + 本流程的具体契约清单。
它写给"不可信边界对面"的 agent 读，只描述规则，绝不包含任何可执行内容。
"""
from __future__ import annotations

from pathlib import Path

from flowrun.ir.schema import FlowIR

__all__ = [
    "AGENTS_STUB_FILENAME",
    "generate_agents_stub",
]

AGENTS_STUB_FILENAME = "AGENTS.stub.md"

_JUDGE_JSON_TEMPLATE = (
    '{"edge":"<候选边编号，形如 D001->H001>","confidence":<0到1之间的数字>,'
    '"evidence":["证据1","证据2"]}'
)


def generate_agents_stub(flow_ir: FlowIR, work_dir: Path) -> Path:
    """在工作区根生成 AGENTS.stub.md，返回其路径（幂等：整体覆盖写）。

    桩内容覆盖任务书 T012 冻结的五要素：packet 协议、完成自报要求、
    out= 契约核对规则、判定任务的严格 JSON 输出格式、workspace 相对路径规则；
    另附本流程各节点声明的产物契约一览，便于 agent 对照自检。
    """
    work_dir = Path(work_dir)
    stub_path = work_dir / AGENTS_STUB_FILENAME
    stub_path.parent.mkdir(parents=True, exist_ok=True)
    stub_path.write_text(_render_stub_text(flow_ir), encoding="utf-8")
    return stub_path


def _render_stub_text(flow_ir: FlowIR) -> str:
    """逐段拼装桩文本（纯字符串拼接，各段落独立成节便于测试断言）。"""
    sections = [
        _render_header(flow_ir),
        _render_packet_protocol_section(),
        _render_self_report_section(),
        _render_contract_rule_section(),
        _render_judge_output_section(),
        _render_path_rule_section(),
        _render_flow_contract_list(flow_ir),
    ]
    return "\n\n".join(sections) + "\n"


def _render_header(flow_ir: FlowIR) -> str:
    return (
        "# AGENTS — FlowRun 工作区引导桩\n\n"
        f"本工作区由 FlowRun 内核驱动执行流程「{flow_ir.run_name}」。\n"
        "你作为 agent 被投递任务包（packet）后，严格按以下协议工作；\n"
        "流程的推进、跳转与人审由内核裁决，你只负责完成被投递的当下这一步。"
    )


def _render_packet_protocol_section() -> str:
    return (
        "## 1. 任务包（packet）协议\n\n"
        "- 任务包在工作区 `packets/` 目录下：任务包 `packets/<节点ID>.packet.md`，"
        "判定包 `packets/<节点ID>.judge.md`。\n"
        "- packet 开头的 frontmatter 只有四个机器字段：\n"
        "  - `node`：本任务对应的节点 ID；\n"
        "  - `run`：流程名；\n"
        "  - `contract`：产物契约（方括号内逗号分隔的文件路径清单）；\n"
        "  - `budget_tokens`：本次流程的 token 预算。\n"
        "- packet 正文只描述「当下这一步」的任务，不包含完整流程图——"
        "不要替内核规划其他节点。"
    )


def _render_self_report_section() -> str:
    return (
        "## 2. 完成后自报\n\n"
        "任务完成后，必须在最终回复中自报：\n"
        "- 改了哪些文件（逐个列出相对本工作区根目录的路径）；\n"
        "- 结果如何（测试/验证的结论，失败也要如实报）。\n"
        "自报内容会进入判定器的证据包，用于后续判定节点的裁决。"
    )


def _render_contract_rule_section() -> str:
    return (
        "## 3. out= 产物契约核对规则\n\n"
        "- frontmatter `contract` 列出的每个路径，都必须以文件形式存在于"
        "工作区根目录下；\n"
        "- 缺任何一件 = 该任务判 fail；只有契约全部满足才算完成；\n"
        "- 不要伪造契约（只建空文件不给内容）——判定器还会结合你的自报与"
        "执行证据综合裁决。"
    )


def _render_judge_output_section() -> str:
    return (
        "## 4. 判定任务的输出格式\n\n"
        "被投递 `*.judge.md` 判定包时，你的最终回复只输出一个严格 JSON 对象，"
        "不要输出任何其他文本：\n\n"
        f"```\n{_JUDGE_JSON_TEMPLATE}\n```\n\n"
        "- `edge` 必须取判定包「候选出边」清单中列出的边编号之一"
        "（形如 `D001->H001`），不能自造目标；\n"
        "- `confidence` 为 0 到 1 之间的数字，低于 0.6 会被判低置信并转人审；\n"
        "- `evidence` 逐条列出你据以裁决的证据。"
    )


def _render_path_rule_section() -> str:
    return (
        "## 5. workspace 相对路径规则\n\n"
        "- 一切产物、契约、引用路径都是相对工作区根目录的相对路径"
        "（如 `src/auth.py`），不要使用工作区之外的绝对路径；\n"
        "- 你的进程以工作区根为当前目录启动，相对读写都以它为基准；\n"
        "- 不要移动或删除契约清单之外的既有文件。"
    )


def _render_flow_contract_list(flow_ir: FlowIR) -> str:
    """本流程各节点声明的产物契约一览（无契约节点不列出）。"""
    contract_lines = [
        f"- {node_id}：{'、'.join(node_spec.outputs)}"
        for node_id, node_spec in flow_ir.nodes.items()
        if node_spec.outputs
    ]
    if not contract_lines:
        contract_lines.append("-（本流程没有节点声明产物契约）")
    return "## 6. 本流程的产物契约一览\n\n" + "\n".join(contract_lines)
