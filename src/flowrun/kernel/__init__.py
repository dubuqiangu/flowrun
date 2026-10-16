"""执行内核（域D）：packet 构建、证据包判定、兜底适配器。

engine / todo_projection / stub_generator 由域D2 后续交付；本包当前导出
域D1（内核判定组件）的冻结 API（judge_decision / EvidencePack /
JudgeVerdict / build_task_packet / build_judge_packet / FallbackAdapter）。
"""
from flowrun.kernel.fallback_adapter import FallbackAdapter
from flowrun.kernel.judge import (
    EvidencePack,
    JudgeVerdict,
    extract_last_verdict,
    is_predicate_edge,
    judge_decision,
)
from flowrun.kernel.packet_builder import (
    build_judge_packet,
    build_task_packet,
    edge_number,
)

__all__ = [
    "FallbackAdapter",
    "EvidencePack",
    "JudgeVerdict",
    "extract_last_verdict",
    "is_predicate_edge",
    "judge_decision",
    "build_judge_packet",
    "build_task_packet",
    "edge_number",
]
