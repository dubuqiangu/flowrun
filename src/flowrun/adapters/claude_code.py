"""Claude Code headless 薄适配器（T007）。

命令模板（design.md §2.2 现场查证结论冻结）：
    claude -p --output-format stream-json
packet 内容默认作为最后一个位置参数注入（registry.expand_command_template，
即 `claude -p "$(cat packet.md)"` 语义）。

语义完成信号（适配器私有判定，interfaces.md §4）：stream-json 行流出现
"type":"result" 消息。依据：Claude Code 的 stream-json 协议在每个回合
结束输出一条 result 事件（官方字段含 session_id / subtype / is_error /
total_cost_usd / usage / duration_ms / num_turns），进程随后自然退出；
先拿语义信号即可在"agent 跑完不退"时宽限 + kill（vibe-kanban 双信号）。
"""
from __future__ import annotations

import json

from flowrun.adapters.base import SignalUpdate, StreamMonitoringAdapter
from flowrun.adapters.registry import register_adapter

# 从 result 消息中提取的已知字段（存在才提取，缺字段不报错——输出按不可信处理）
_RESULT_MESSAGE_FIELDS = (
    "session_id",
    "subtype",
    "is_error",
    "total_cost_usd",
    "duration_ms",
    "num_turns",
)


@register_adapter
class ClaudeCodeAdapter(StreamMonitoringAdapter):
    name = "claude-code"
    command_heads = ("claude",)
    default_command_template = ["claude", "-p", "--output-format", "stream-json"]

    def parse_line(self, line: str) -> SignalUpdate:
        stripped_line = line.strip()
        if not stripped_line.startswith("{"):
            return SignalUpdate()
        try:
            stream_message = json.loads(stripped_line)
        except json.JSONDecodeError:
            return SignalUpdate()
        if not isinstance(stream_message, dict):
            return SignalUpdate()
        if stream_message.get("type") != "result":
            return SignalUpdate()

        structured_fields = {
            result_field: stream_message[result_field]
            for result_field in _RESULT_MESSAGE_FIELDS
            if result_field in stream_message
        }
        usage_summary = stream_message.get("usage")
        if isinstance(usage_summary, dict):
            structured_fields["usage"] = usage_summary
        return SignalUpdate(semantic_done=True, structured_fields=structured_fields)
