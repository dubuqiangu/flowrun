"""兜底适配器（域D）：未注册命令头的通用投递通道。

registry.create_adapter_for_command 对未知命令头返回 None（长尾 CLI 没有声明式
适配器时），内核改用本类：不解析任何语义完成信号，semantic_done 恒 False，
完成判定退化为"退出码 + 总超时"二信号（design.md §2.2 vibe-kanban 双信号）。

T020f（长尾 CLI 的用量入账）：兜底适配器此前 parse_line 恒不产 structured_result，
于是 task 路径 ``agent_exited.payload`` 里没有 ``tokens``——budget 闸对长尾 CLI 的
真实花费完全失明（计量错误：账不是"少记一笔"，而是把一笔真实消耗记成 0）。本类
补一件**与语义完成信号无关**的事：按 flowrun 自己声明的约定提取 token 用量，搬进
``structured_result``，键名与数字语义都与判定/任务路径完全一致（唯一实现是
``kernel.engine_evidence.extract_usage_tokens``，本类只搬运，不另立一套口径）。

用量约定（**由 flowrun 声明；不依赖任何 agent 内部 API，不解析任何厂商文案**）：
stdout 某一行里**最后一个**可解析的顶层 JSON 对象，若含 ``usage`` 或
``total_tokens`` 顶层键（值须为明细 dict 或数字），就把该键原样搬进
``structured_result``。例如：

    {"usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}}

多次声明时后行覆盖前行（基类用 ``structured_result.update`` 合并），即"流中最后
一次声明为准"；不含这两个键的行一律不产任何结构化字段，普通 CLI 日志不会意外
污染 structured_result。

安全性与 base 一致：子进程原始输出只落盘审计，本类只在内存里做结构化读取
（绝不 eval/执行，interfaces.md §7）；信号位恒 False，绝不因用量声明而改变
"退出码 + 超时"的完成判定。
"""
from __future__ import annotations

import json

from flowrun.adapters.base import SignalUpdate, StreamMonitoringAdapter

__all__ = ["FallbackAdapter"]

# 用量声明键 = extract_usage_tokens 消费的键名（不得另立一套键名）
DECLARED_USAGE_KEYS = ("usage", "total_tokens")


class FallbackAdapter(StreamMonitoringAdapter):
    """未知命令头的兜底适配器：永不置位 semantic_done，只搬运用量声明。"""

    name = "fallback"

    def parse_line(self, line: str) -> SignalUpdate:
        declared_usage_fields = _declared_usage_fields(_last_json_object(line))
        if not declared_usage_fields:
            return SignalUpdate()
        return SignalUpdate(structured_fields=declared_usage_fields)


def _last_json_object(line: str) -> dict | None:
    """行内**最后一个**可解析为 JSON 对象的候选（与判定路径"取末个 JSON"同序）。

    候选来自平衡花括号扫描（字符串感知，见 ``_balanced_json_candidates``）；
    从后往前取第一个能 ``json.loads`` 成 dict 的候选。整行没有可解析对象时返回
    None（普通日志行）。
    """
    for json_candidate in reversed(_balanced_json_candidates(line)):
        try:
            parsed_object = json.loads(json_candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed_object, dict):
            return parsed_object
    return None


def _declared_usage_fields(json_object: dict | None) -> dict:
    """取该 JSON 对象里我们声明过的用量键（存在且值可用者），其余键一概不搬。"""
    if json_object is None:
        return {}
    declared_fields: dict = {}
    for usage_key in DECLARED_USAGE_KEYS:
        if usage_key not in json_object:
            continue
        usage_value = json_object[usage_key]
        if _is_usable_usage_value(usage_value):
            declared_fields[usage_key] = usage_value
    return declared_fields


def _is_usable_usage_value(usage_value: object) -> bool:
    """用量键的值是否可用：明细 dict，或单个数字（bool 不是 token 数）。

    与 ``extract_usage_tokens`` 同口径——bool / 字符串等不可用值直接挡在
    structured_result 之外（即便搬进去也会被归一为 0，挡住是为了让结构化结果
    只承载真实的用量事实）。
    """
    if isinstance(usage_value, dict):
        return True
    return isinstance(usage_value, (int, float)) and not isinstance(usage_value, bool)


def _balanced_json_candidates(line: str) -> list[str]:
    """行内全部平衡花括号的顶层 ``{...}`` 子串（字符串感知）。

    字符串内的花括号（如 ``{"note": "a } b"}``）是合法 JSON 内容，只按深度计花
    括号会把整段误配并丢掉；故只有候选花括号已开始（深度 > 0）才进入字符串态，
    避免 JSON 之前的散文引号把后续内容整片吞掉。误配片段会在 json.loads 处被
    丢弃，只解析不执行，无安全面。
    """
    candidates: list[str] = []
    brace_depth = 0
    candidate_start_position: int | None = None
    inside_string = False
    escape_pending = False
    for character_position, character in enumerate(line):
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
                    candidates.append(line[candidate_start_position : character_position + 1])
                    candidate_start_position = None
        elif character == '"' and brace_depth > 0:
            inside_string = True
    return candidates
