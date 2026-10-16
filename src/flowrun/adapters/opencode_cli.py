"""opencode headless 薄适配器（T008；M2 再评估切 ACP）。

命令模板（design.md §2.2 现场查证结论冻结）：
    opencode run --format json
packet 内容默认作为最后一个位置参数注入（registry.expand_command_template）。

语义完成信号（适配器私有判定，interfaces.md §4）——**依据与不确定性，
供 orchestrator 复核**：`opencode run --format json` 的确切输出样例未进入
本次冻结件，按 opencode headless 公开行为做两类合理形态假设，两者都认作
语义完成：

1. 事件形态：行流 JSON 带 "type" 字段且取值为会话空闲/结束类事件
   （session.idle / session.end / message.completed / result）；
2. 终末消息形态：一条完整 JSON 消息对象，role="assistant" 且含 "parts"
   （`run --format json` 的最终回复即一条消息对象，含 id/cost/usage 等
   字段——字段名 camel/snake 两种拼写都做防御性提取）。

若真机输出两种形态都不匹配，本适配器退化为"退出码 + 超时"二信号
（基类语义仍成立，判定闭环不受影响），仅损失"进程跑完不退"场景的
提前回收。复核方式：真机跑 `opencode run "hi" --format json` 留样例
后收敛 _COMPLETION_EVENT_TYPES 与终末消息判定。
"""
from __future__ import annotations

import json

from flowrun.adapters.base import SignalUpdate, StreamMonitoringAdapter
from flowrun.adapters.registry import register_adapter

_COMPLETION_EVENT_TYPES = frozenset(
    {"session.idle", "session.end", "message.completed", "result"}
)
# 从终末消息/完成事件中防御性提取的已知字段（camel/snake 双拼写都认）
_RESULT_MESSAGE_FIELDS = (
    "id",
    "sessionID",
    "session_id",
    "modelID",
    "model_id",
    "cost",
    "tokens",
    "usage",
)


@register_adapter
class OpenCodeCliAdapter(StreamMonitoringAdapter):
    name = "opencode"
    command_heads = ("opencode",)
    default_command_template = ["opencode", "run", "--format", "json"]

    def parse_line(self, line: str) -> SignalUpdate:
        stripped_line = line.strip()
        if not stripped_line.startswith("{"):
            return SignalUpdate()
        try:
            output_message = json.loads(stripped_line)
        except json.JSONDecodeError:
            return SignalUpdate()
        if not isinstance(output_message, dict):
            return SignalUpdate()

        if self._is_completion_event(output_message):
            return SignalUpdate(
                semantic_done=True,
                structured_fields=self._extract_known_fields(output_message),
            )
        if self._is_final_assistant_message(output_message):
            return SignalUpdate(
                semantic_done=True,
                structured_fields=self._extract_known_fields(output_message),
            )
        return SignalUpdate()

    @staticmethod
    def _is_completion_event(output_message: dict) -> bool:
        event_type = output_message.get("type")
        return isinstance(event_type, str) and event_type in _COMPLETION_EVENT_TYPES

    @staticmethod
    def _is_final_assistant_message(output_message: dict) -> bool:
        return (
            output_message.get("role") == "assistant"
            and isinstance(output_message.get("parts"), list)
        )

    @staticmethod
    def _extract_known_fields(output_message: dict) -> dict:
        return {
            result_field: output_message[result_field]
            for result_field in _RESULT_MESSAGE_FIELDS
            if result_field in output_message
        }
