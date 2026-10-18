"""FlowRun 域D2 · 判定证据包与 verdict 归一化（design.md §5.2 谓词判定调用约定）。

EvidencePack 是内核交给判定 hook（``EngineHooks.judge_decision``）的证据载荷：
入边来源节点的最近一次投递结果（退出码 / 双信号 / 产物契约核对），全部从
ledger 事件现算（重放等价），本进程内的 DispatchResult 只补充审计文件名。

注：任务书冻结接口里 EvidencePack/JudgeVerdict 由域D1 的 kernel.judge 定义，
本域不 import 域D1 文件（并行开发纪律），故此处定义**结构等价**的引擎侧
数据类；生产装配（cli）如需域D1 的类，在 hook 里做一层适配即可，
orchestrator 裁决后可统一。verdict 归一化同时接受数据类（按属性读
tripped/chosen_edge_target/confidence/evidence）与 dict（按键读），测试
stub 与域D1 实现都能直接喂给引擎。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from flowrun.ir.schema import FlowIR, NodeSpec
from flowrun.state.flow_state import FlowState
from flowrun.state.ledger import EventLedger

from .engine_gates import latest_event_payload
from .engine_graph import incoming_edges

__all__ = [
    "UpstreamNodeEvidence",
    "EvidencePack",
    "VerdictView",
    "assemble_evidence_pack",
    "normalize_verdict",
    "extract_usage_tokens",
]


@dataclass
class UpstreamNodeEvidence:
    """判定节点某条入边来源节点的证据快照。"""

    node_id: str
    exit_code: int | None
    timed_out: bool
    semantic_done: bool
    expected_outputs: list[str]
    present_outputs: list[str]
    stdout_file_name: str | None = None  # 原始输出审计文件名（仅本进程内可见时非空）


@dataclass
class EvidencePack:
    """判定调用的完整证据包（判定提示所需的上游事实，全部结构化）。"""

    decision_node_id: str
    upstream_evidence: list[UpstreamNodeEvidence] = field(default_factory=list)


@dataclass
class VerdictView:
    """归一化后的判定结论（域D1 JudgeVerdict 或 dict 的统一读法）。"""

    tripped: bool
    chosen_edge_target: str | None
    confidence: float | None
    evidence: list


def assemble_evidence_pack(
    flow_ir: FlowIR,
    decision_node: NodeSpec,
    flow_state: FlowState,
    ledger: EventLedger,
    workspace: Path,
    dispatch_results: dict,
) -> EvidencePack:
    """组装判定节点的证据包：逐条入边取来源节点的投递结果与产物核对。

    只信任 ledger（重放等价）：退出码/双信号取来源节点最近的 agent_exited
    事件；产物核对以 workspace 为根逐项检查 out= 契约的存在性。
    dispatch_results（本进程内最近一次 DispatchResult）仅用于补充审计文件名。
    """
    upstream_evidence: list[UpstreamNodeEvidence] = []
    for edge in incoming_edges(flow_ir, decision_node.node_id):
        if edge.source_id not in flow_ir.nodes:
            continue  # 编译器已保证端点存在，防御性跳过
        source_node = flow_ir.nodes[edge.source_id]
        exit_payload = latest_event_payload(ledger, "agent_exited", source_node.node_id) or {}
        expected_outputs = list(source_node.outputs)
        present_outputs = [
            output_path
            for output_path in expected_outputs
            if (workspace / output_path).exists()
        ]
        recent_dispatch_result = dispatch_results.get(source_node.node_id)
        stdout_file_name = (
            recent_dispatch_result.stdout_path.name
            if recent_dispatch_result is not None
            else None
        )
        upstream_evidence.append(
            UpstreamNodeEvidence(
                node_id=source_node.node_id,
                exit_code=exit_payload.get("exit_code"),
                timed_out=bool(exit_payload.get("timed_out")),
                semantic_done=bool(exit_payload.get("semantic_done")),
                expected_outputs=expected_outputs,
                present_outputs=present_outputs,
                stdout_file_name=stdout_file_name,
            )
        )
    return EvidencePack(decision_node_id=decision_node.node_id, upstream_evidence=upstream_evidence)


def normalize_verdict(verdict: object) -> VerdictView:
    """把域D1 的 JudgeVerdict 数据类或 dict 归一成引擎统一读取的 VerdictView。

    缺字段按保守值处理：tripped 缺省 False，chosen_edge_target 缺省 None
    （None 不在任何出边集合内 → 必然落 illegal_edge 闸，宁停不越权）。
    """
    if isinstance(verdict, dict):
        return VerdictView(
            tripped=bool(verdict.get("tripped", False)),
            chosen_edge_target=verdict.get("chosen_edge_target"),
            confidence=verdict.get("confidence"),
            evidence=list(verdict.get("evidence") or []),
        )
    return VerdictView(
        tripped=bool(getattr(verdict, "tripped", False)),
        chosen_edge_target=getattr(verdict, "chosen_edge_target", None),
        confidence=getattr(verdict, "confidence", None),
        evidence=list(getattr(verdict, "evidence", None) or []),
    )


TOTAL_SEMANTIC_USAGE_KEYS = frozenset({"total_tokens", "total"})
"""usage dict 中表示"已含分量"的总数键：优先取用，求和时排除，避免重复计数。"""


def _as_token_count(value: object) -> int | None:
    """把外部数值归一成 int token 数；bool / 非数值 / NaN 一律视为不可用（None）。"""
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and value != value:  # NaN：无法作为 token 数
        return None
    return int(value)


def extract_usage_tokens(structured_result: dict) -> int:
    """从 DispatchResult.structured_result 提取本次投递消耗的 token 数。

    约定（任务书）：structured_result["usage"] 里的数字进 payload.tokens。
    usage 为 dict 时**含 total 时优先取 total，不重复累加分量**：
    先看 ``total_tokens``，再退到 ``total``；两者都不是数字时才把其余顶层
    数字叶子求和（求和时排除 ``total_tokens`` / ``total`` 键，嵌套 dict 不递归）。
    这样 ``{"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}`` 得 30
    而不是 60。usage 为单个数字时直接取值；兼容 usage 缺失时的顶层
    ``total_tokens``。提取不到返回 0（不落 tokens 字段，FlowState 不累加）。
    所有外部输入只做结构化读取，绝不 eval。
    """
    if not isinstance(structured_result, dict):
        return 0
    usage_value = structured_result.get("usage")
    if isinstance(usage_value, bool):
        return 0
    if isinstance(usage_value, (int, float)):
        return int(usage_value)
    if isinstance(usage_value, dict):
        for total_key in ("total_tokens", "total"):
            total_tokens_value = _as_token_count(usage_value.get(total_key))
            if total_tokens_value is not None:
                return total_tokens_value
        return sum(
            int(leaf_value)
            for leaf_key, leaf_value in usage_value.items()
            if leaf_key not in TOTAL_SEMANTIC_USAGE_KEYS
            and isinstance(leaf_value, (int, float))
            and not isinstance(leaf_value, bool)
        )
    total_tokens_value = structured_result.get("total_tokens")
    if isinstance(total_tokens_value, (int, float)) and not isinstance(total_tokens_value, bool):
        return int(total_tokens_value)
    return 0
