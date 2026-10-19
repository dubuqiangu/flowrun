"""verdict 文本解析：从 agent stdout 里提取最后一个合法 verdict JSON。

从 ``flowrun.kernel.judge`` 拆出（纯结构调整，行为零变化）：判定协议（构建
packet / 投递 / 校验 / 重试 / 落账）与"从不可信文本里挑出 JSON"是两件事，
后者安全敏感（只解析、绝不执行），单独成模块更容易被审阅与复用。

``flowrun.kernel.judge`` 会把这里的公开名字原样重新导出，历史导入路径
``from flowrun.kernel.judge import extract_last_verdict`` 保持可用。
"""
from __future__ import annotations

import json

__all__ = [
    "extract_last_verdict",
    "collect_balanced_json_objects",
    "last_json_object",
]


def extract_last_verdict(stdout_text: str) -> dict | None:
    """从审计 stdout 提取**最后一个**合法 verdict JSON 对象，返回其字段 dict。

    合法 = 可解析的 JSON 对象，且 edge 为字符串、confidence 为数字（bool
    除外）、evidence 缺失时按空列表处理。找不到返回 None。

    实现：平衡花括号扫描收集全部顶层 {...} 子串，从后往前取第一个合法者
    ——agent 常在 JSON 前打印思考文本、或多段 JSON，只认最后一个。
    """
    json_candidates = collect_balanced_json_objects(stdout_text)
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


def last_json_object(stdout_text: str) -> dict | None:
    """stdout 里最后一个可解析的 JSON 对象（**不看它是否构成合法 verdict**）。

    与 ``extract_last_verdict`` 扫同一批候选：判定 agent 即便给出非法 verdict
    （边编号不命中 / confidence 过低），这次投递的成本也真实发生了，故用量提取
    独立于 verdict 校验。
    """
    for json_candidate in reversed(collect_balanced_json_objects(stdout_text)):
        try:
            parsed_object = json.loads(json_candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed_object, dict):
            return parsed_object
    return None


def collect_balanced_json_objects(stdout_text: str) -> list[str]:
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