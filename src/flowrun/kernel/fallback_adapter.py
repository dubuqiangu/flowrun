"""兜底适配器（域D）：未注册命令头的通用投递通道。

registry.create_adapter_for_command 对未知命令头返回 None（长尾 CLI 没有声明式
适配器时），内核改用本类：不解析任何语义完成信号，semantic_done 恒 False，
完成判定退化为"退出码 + 总超时"二信号（design.md §2.2 vibe-kanban 双信号）。

安全性与 base 一致：子进程原始输出只落盘审计，不进任何结构化提取。
"""
from __future__ import annotations

from flowrun.adapters.base import SignalUpdate, StreamMonitoringAdapter

__all__ = ["FallbackAdapter"]


class FallbackAdapter(StreamMonitoringAdapter):
    """未知命令头的兜底适配器：parse_line 永不置位 semantic_done。"""

    name = "fallback"

    def parse_line(self, line: str) -> SignalUpdate:
        return SignalUpdate()
