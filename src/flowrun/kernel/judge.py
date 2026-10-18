"""证据包判定器（tasks.md T009，design.md §5.2 谓词判定调用约定）。

控制流归内核，语义判断归模型：判定节点处，判定 agent 只回答一个结构化
verdict（严格 JSON），本模块负责完整判定协议：

1. 从 IR 过滤判定节点的候选谓词出边（when:all barrier 边与 timeout 边
   不进候选——已裁决：判定器不评估 timeout 边，定时器门控归引擎）；
2. 构建判定 packet（候选边编号 + when 原文 + 证据包 + JSON 输出要求）；
3. 经注入的 resolve_adapter / resolve_command 拿到判定 agent 的适配器与
   命令模板并投递（decision_node.agent 为 None 时用 flow_ir.default_agent）；
4. 从 stdout 审计文件提取**最后一个** JSON 对象作为 verdict；
5. 校验：verdict.edge 必须命中候选边编号集合（"{source}->{target}" 文本）；
   confidence 必须落在 [0.6, 1]；不满足重试 1 次（重新投递）；
6. 两次都失败 → 落 gate_tripped 事件（payload.gate="verdict"）并返回
   tripped=True（design.md §5.3 第三闸：非法边转移，暂停转人审）。

落账边界（与域D2 engine 的约定）：判定成功**不落任何账**——verdict /
edge_taken 由 engine 落（单一写路径）；只有判定失败落 gate_tripped，
且 idem 的 attempt 段按本节点已有 gate_tripped 条数递增，重复调用不会
被幂等去重吞掉。

投递用量回传（tasks.md T020a）：判定投递的 token 消耗必须可见、可审计，但落账仍
归 engine（单一写路径）——本模块只把投递事实放在 ``JudgeVerdict.dispatch_facts`` 里
回传，由 engine 按 task 路径同口径落 ``agent_exited``（payload 带 ``tokens``）。

安全（interfaces.md §7）：agent stdout 是不可信输入——只进结构化 JSON
提取器，绝不 eval / 执行。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from flowrun.adapters.base import AgentAdapter, DispatchResult
from flowrun.ir.schema import EdgeSpec, FlowIR, NodeSpec
# 用量提取单一来源：task 路径（engine_node_executors）用的是同一个函数，判定路径
# "按既有口径入账"才不会有语义漂移（导入方向 D1→D2 单向，无环）。
from flowrun.kernel.engine_evidence import extract_usage_tokens
from flowrun.kernel.packet_builder import build_judge_packet, edge_number
from flowrun.state.ledger import EventLedger

# barrier 边标记（编译裁决：多入边节点的 when:all，写在入边或出边上）
WHEN_ALL_MARKER = "all"
# verdict 置信度门槛（design.md §5.2：confidence < 0.6 重试一次）
VERDICT_CONFIDENCE_THRESHOLD = 0.6
# 判定重试次数（design.md §5.2：重试 1 次）
VERDICT_RETRY_LIMIT = 1
# gate_tripped 事件类型（域B 冻结的 17 种事件之一）
GATE_TRIPPED_EVENT_TYPE = "gate_tripped"

__all__ = [
    "EvidencePack",
    "JudgeDispatchFacts",
    "JudgeVerdict",
    "is_predicate_edge",
    "extract_last_verdict",
    "judge_decision",
]


@dataclass
class EvidencePack:
    """判定输入证据包（字段名冻结，域D2 engine 按此组装）。"""

    node_id: str
    exit_code: int | None
    timed_out: bool
    semantic_done: bool
    duration_seconds: float
    outputs_present: dict[str, bool]  # node.outputs 每项相对 work_dir 的存在性
    audit_tail: str                  # agent stdout 审计文件最后 ~2000 字符


@dataclass(frozen=True)
class JudgeDispatchFacts:
    """判定 agent 投递的审计事实（tasks.md T020a：判定消耗也必须入账）。

    一次判定协议最多投递 ``1 + VERDICT_RETRY_LIMIT`` 次：``tokens`` 是各次投递的
    **合计**（重试同样是真实成本）；``exit_code`` / ``timed_out`` / ``semantic_done``
    取**最后一次**投递（与 task 路径"一次投递一条 agent_exited"的口径对齐）。判定未
    真正投递（探活失败 / 无候选边）时不构造本对象（回传 None）。
    """

    tokens: int = 0
    exit_code: int | None = None
    timed_out: bool = False
    semantic_done: bool = False


@dataclass
class JudgeVerdict:
    """judge_decision 的返回值（tripped=True 时 gate_tripped 事件已由本函数落账）。"""

    tripped: bool                  # True=判定失败已落 gate_tripped 事件
    chosen_edge_target: str | None  # 命中的出边 target_id；tripped 时为 None
    confidence: float               # 命中时为 verdict.confidence；tripped 时为 0.0
    evidence: list[str]             # 命中时为 verdict.evidence；tripped 时为 [失败原因]
    dispatch_facts: JudgeDispatchFacts | None = None  # 投递事实（T020a）；None=未真实投递


def is_predicate_edge(edge: EdgeSpec) -> bool:
    """谓词边 = 判定候选：有自然语言 when、非 barrier、非 timeout 边。

    timeout 边按编译契约是 when=None + timeout_seconds 非空；此处再显式
    排除"带 when 又带 timeout"的畸形边，防御上游改动。
    """
    return (
        edge.when is not None
        and edge.when != WHEN_ALL_MARKER
        and edge.timeout_seconds is None
    )


async def judge_decision(
    flow_ir: FlowIR,
    decision_node: NodeSpec,
    evidence_pack: EvidencePack,
    work_dir: Path,
    packet_dir: Path,
    ledger: EventLedger,
    resolve_adapter: Callable[[str], AgentAdapter | None],
    resolve_command: Callable[[str], list[str]],
    timeout_seconds: float | None = None,
) -> JudgeVerdict:
    """执行一个判定节点的完整判定协议（构建 packet → 投递 → 提取 → 校验 → 重试/落账）。

    resolve_adapter / resolve_command 由域D2 注入（registry / probe 的包装），
    传入 agent 名，返回该 agent 的适配器实例与命令模板。成功不落账；投递用量
    随返回值回传给 engine（``dispatch_facts``，T020a），落账仍归 engine 单一写路径。
    """
    candidate_edges = [
        edge
        for edge in flow_ir.edges
        if edge.source_id == decision_node.node_id and is_predicate_edge(edge)
    ]
    run_name = flow_ir.run_name
    if not candidate_edges:
        return _trip_verdict_gate(
            ledger,
            decision_node,
            run_name=run_name,
            candidate_edge_numbers=[],
            failure_reason="判定节点没有候选谓词边（when:all/timeout/无条件边均不参与判定）",
        )

    judge_agent_name = decision_node.agent or flow_ir.default_agent
    adapter = resolve_adapter(judge_agent_name)
    agent_command = resolve_command(judge_agent_name)
    if adapter is None:
        return _trip_verdict_gate(
            ledger,
            decision_node,
            run_name=run_name,
            candidate_edge_numbers=[edge_number(edge) for edge in candidate_edges],
            failure_reason=f"判定 agent '{judge_agent_name}' 没有可用适配器",
        )
    if not agent_command:
        return _trip_verdict_gate(
            ledger,
            decision_node,
            run_name=run_name,
            candidate_edge_numbers=[edge_number(edge) for edge in candidate_edges],
            failure_reason=f"判定 agent '{judge_agent_name}' 命令模板为空",
        )

    packet_path = build_judge_packet(
        flow_ir, decision_node, candidate_edges, evidence_pack, packet_dir
    )
    candidate_edge_numbers = [edge_number(edge) for edge in candidate_edges]
    failure_reason: str | None = None
    judge_dispatch = JudgeDispatchFacts()
    for _attempt_round in range(1 + VERDICT_RETRY_LIMIT):
        dispatch_result = await adapter.dispatch(
            packet_path, agent_command, Path(work_dir), timeout_seconds
        )
        stdout_text = _read_audit_stdout(dispatch_result.stdout_path)
        judge_dispatch = _accumulate_dispatch_facts(
            judge_dispatch, dispatch_result, stdout_text
        )
        verdict = extract_last_verdict(stdout_text)
        if verdict is None:
            failure_reason = "stdout 审计中未找到可解析的 JSON verdict"
            continue
        if verdict["edge"] not in candidate_edge_numbers:
            failure_reason = (
                f"verdict.edge '{verdict['edge']}' 未命中候选边编号集合 "
                f"{candidate_edge_numbers}"
            )
            continue
        if not (VERDICT_CONFIDENCE_THRESHOLD <= verdict["confidence"] <= 1.0):
            failure_reason = (
                f"verdict.confidence {verdict['confidence']} 不在 "
                f"[{VERDICT_CONFIDENCE_THRESHOLD}, 1] 区间"
            )
            continue
        chosen_edge_target = _target_of_edge_number(verdict["edge"], candidate_edges)
        return JudgeVerdict(
            tripped=False,
            chosen_edge_target=chosen_edge_target,
            confidence=verdict["confidence"],
            evidence=verdict["evidence"],
            dispatch_facts=judge_dispatch,
        )
    return _trip_verdict_gate(
        ledger,
        decision_node,
        run_name=run_name,
        candidate_edge_numbers=candidate_edge_numbers,
        failure_reason=failure_reason or "未知判定失败",
        dispatch_facts=judge_dispatch,
    )


def extract_last_verdict(stdout_text: str) -> dict | None:
    """从审计 stdout 提取**最后一个**合法 verdict JSON 对象，返回其字段 dict。

    合法 = 可解析的 JSON 对象，且 edge 为字符串、confidence 为数字（bool
    除外）、evidence 缺失时按空列表处理。找不到返回 None。

    实现：平衡花括号扫描收集全部顶层 {...} 子串，从后往前取第一个合法者
    ——agent 常在 JSON 前打印思考文本、或多段 JSON，只认最后一个。
    """
    json_candidates = _collect_balanced_json_objects(stdout_text)
    for json_candidate in reversed(json_candidates):
        try:
            parsed_object = json.loads(json_candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed_object, dict):
            continue
        edge_value = parsed_object.get("edge")
        confidence_value = parsed_object.get("confidence")
        if not isinstance(edge_value, str):
            continue
        if isinstance(confidence_value, bool) or not isinstance(confidence_value, (int, float)):
            continue
        raw_evidence = parsed_object.get("evidence", [])
        if not isinstance(raw_evidence, list):
            raw_evidence = []
        return {
            "edge": edge_value,
            "confidence": float(confidence_value),
            "evidence": [str(evidence_item) for evidence_item in raw_evidence],
        }
    return None


# ---------------------------------------------------------------- 内部实现


def _collect_balanced_json_objects(stdout_text: str) -> list[str]:
    """收集文本里全部平衡花括号的顶层 {...} 子串（K4：字符串感知）。

    扫描时跟踪"是否在 JSON 字符串内 + 转义状态"，**只在字符串外**计花括号
    深度——``evidence: ["a } b"]`` 这类字符串内的花括号是合法 JSON，旧实现
    会误配并丢掉整段判定。只有候选花括号已开始（depth > 0）才进入字符串态，
    因此 JSON 之前的散文（引号不可信）不会把后续内容整片吞掉。

    误配片段仍会在 json.loads 处被丢弃，不产生安全问题——只解析不执行。
    """
    json_candidates: list[str] = []
    brace_depth = 0
    candidate_start_position: int | None = None
    inside_string = False
    escape_pending = False
    for character_position, character in enumerate(stdout_text):
        if inside_string:
            if escape_pending:
                escape_pending = False
            elif character == "\\":
                escape_pending = True
            elif character == '"':
                inside_string = False
            continue
        if character == "{":
            if brace_depth == 0:
                candidate_start_position = character_position
            brace_depth += 1
        elif character == "}":
            if brace_depth > 0:
                brace_depth -= 1
                if brace_depth == 0 and candidate_start_position is not None:
                    json_candidates.append(
                        stdout_text[candidate_start_position : character_position + 1]
                    )
                    candidate_start_position = None
        elif character == '"' and brace_depth > 0:
            inside_string = True
    return json_candidates


def _read_audit_stdout(stdout_path: Path) -> str:
    """读判定 agent 的 stdout 审计文件（不可信输入，只做结构化提取）。"""
    try:
        return Path(stdout_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _accumulate_dispatch_facts(
    previous_facts: JudgeDispatchFacts,
    dispatch_result: DispatchResult,
    stdout_text: str,
) -> JudgeDispatchFacts:
    """把本次投递累加进判定投递事实（tokens 累加、退出信号取本次）。"""
    return JudgeDispatchFacts(
        tokens=previous_facts.tokens + _dispatch_usage_tokens(dispatch_result, stdout_text),
        exit_code=dispatch_result.exit_code,
        timed_out=bool(dispatch_result.timed_out),
        semantic_done=bool(dispatch_result.semantic_done),
    )


def _dispatch_usage_tokens(dispatch_result: DispatchResult, stdout_text: str) -> int:
    """本次投递的 token 数：适配层结构化结果优先，缺失退审计 stdout 的末个 JSON。

    两路只取一（同一笔成本可能两边都看得见，相加即重复计数）；数字口径统一走
    ``extract_usage_tokens``（``total_tokens`` / ``total`` 优先，分量不重复累加）。
    """
    structured_usage_tokens = extract_usage_tokens(dispatch_result.structured_result)
    if structured_usage_tokens > 0:
        return structured_usage_tokens
    last_json_object = _last_json_object(stdout_text)
    return extract_usage_tokens(last_json_object) if last_json_object is not None else 0


def _last_json_object(stdout_text: str) -> dict | None:
    """stdout 里最后一个可解析的 JSON 对象（**不看它是否构成合法 verdict**）。

    与 ``extract_last_verdict`` 扫同一批候选：判定 agent 即便给出非法 verdict
    （边编号不命中 / confidence 过低），这次投递的成本也真实发生了，故用量提取
    独立于 verdict 校验。
    """
    for json_candidate in reversed(_collect_balanced_json_objects(stdout_text)):
        try:
            parsed_object = json.loads(json_candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed_object, dict):
            return parsed_object
    return None


def _target_of_edge_number(matched_edge_number: str, candidate_edges: list[EdgeSpec]) -> str:
    """把命中的边编号映射回出边 target_id（编号已命中集合，必然找得到）。"""
    for candidate_edge in candidate_edges:
        if edge_number(candidate_edge) == matched_edge_number:
            return candidate_edge.target_id
    raise LookupError(f"边编号 '{matched_edge_number}' 不在候选边集合内（不应到达）")


def _trip_verdict_gate(
    ledger: EventLedger,
    decision_node: NodeSpec,
    run_name: str,
    candidate_edge_numbers: list[str],
    failure_reason: str,
    dispatch_facts: JudgeDispatchFacts | None = None,
) -> JudgeVerdict:
    """落 gate_tripped（gate="verdict"）事件并返回 tripped=True 的判定结果。

    idem 的 attempt 段 = 本节点此前已落账的 gate_tripped 条数 + 1，引擎对
    同一节点重复驱动判定时每次失败都能留下独立审计记录（不被幂等去重合并）。
    投递前的失败（无候选边 / 无适配器 / 探活拦下）没有投递事实，传 None。
    """
    prior_gate_trips = sum(
        1
        for entry in ledger.entries()
        if entry.type == GATE_TRIPPED_EVENT_TYPE and entry.node == decision_node.node_id
    )
    attempt_number = prior_gate_trips + 1
    ledger.append(
        GATE_TRIPPED_EVENT_TYPE,
        {
            "gate": "verdict",
            "actual": failure_reason,
            "limit": (
                f"verdict.edge 必须命中候选边编号集合 {candidate_edge_numbers} 且 "
                f"confidence >= {VERDICT_CONFIDENCE_THRESHOLD}"
            ),
        },
        idem_key=(
            f"{run_name}-{decision_node.node_id}"
            f"-{GATE_TRIPPED_EVENT_TYPE}-a{attempt_number}"
        ),
        run_name=run_name,
        node_id=decision_node.node_id,
    )
    return JudgeVerdict(
        tripped=True,
        chosen_edge_target=None,
        confidence=0.0,
        evidence=[failure_reason],
        dispatch_facts=dispatch_facts,
    )
