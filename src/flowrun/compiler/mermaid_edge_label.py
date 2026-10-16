"""Edge-label parsing for the Mermaid flowchart subset: ``|when: ...|``.

Kept apart from mermaid_syntax (which owns node shapes and ';;' config) so
each module covers one responsibility. Every error carries the Mermaid source
line number.
"""

from __future__ import annotations

import re

from flowrun.compiler.mermaid_errors import MermaidParseError

WHEN_ALL = "all"

_TIMEOUT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(s|m|h)?")
_TIMEOUT_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0, None: 1.0}


def parse_edge_label(
    label_text: str | None, line_number: int
) -> tuple[str | None, int, float | None]:
    """Parse '|when: fail, retry: 2, timeout: 30m|' into (when, retry, timeout_seconds)."""
    when_value: str | None = None
    retry_count = 0
    timeout_seconds: float | None = None
    if label_text is None:
        return when_value, retry_count, timeout_seconds
    if not label_text:
        raise MermaidParseError(
            "edge label '||' is empty (expected 'when: ...', 'retry: N' or 'timeout: ...')",
            line_number,
        )
    seen_keys: set[str] = set()
    for label_item in label_text.split(","):
        label_item = label_item.strip()
        if not label_item:
            raise MermaidParseError(
                f"edge label '{label_text}' contains an empty item", line_number
            )
        label_key, separator, label_value = label_item.partition(":")
        label_key = label_key.strip().lower()
        label_value = label_value.strip()
        if not separator or not label_key:
            raise MermaidParseError(
                f"invalid edge label item '{label_item}' "
                "(expected 'key: value' with key in when/retry/timeout)",
                line_number,
            )
        if label_key in seen_keys:
            raise MermaidParseError(
                f"duplicate '{label_key}:' in edge label '{label_text}'", line_number
            )
        seen_keys.add(label_key)
        if label_key == "when":
            if not label_value:
                raise MermaidParseError(
                    "'when:' needs a condition (e.g. 'when: all' or a natural-language predicate)",
                    line_number,
                )
            # 'all'/'ALL'/'All' 都是 barrier；归一为小写，避免大小写变体被当成
            # 自然语言谓词静默通过（存值统一写 'all'）。其余谓词保留原文。
            when_value = WHEN_ALL if label_value.casefold() == WHEN_ALL else label_value
        elif label_key == "retry":
            if not re.fullmatch(r"\d+", label_value):
                raise MermaidParseError(
                    f"invalid retry count '{label_value}' "
                    "(expected a non-negative integer like 'retry: 2')",
                    line_number,
                )
            retry_count = int(label_value)
        elif label_key == "timeout":
            timeout_seconds = parse_timeout_duration(label_value, line_number)
        else:
            raise MermaidParseError(
                f"unknown edge label key '{label_key}' (supported: when, retry, timeout)",
                line_number,
            )
    return when_value, retry_count, timeout_seconds


def parse_timeout_duration(label_value: str, line_number: int) -> float:
    """Parse '30m' / '45s' / '2h' / '90' into seconds; the value must be > 0."""
    timeout_match = _TIMEOUT_PATTERN.fullmatch(label_value)
    if timeout_match is None:
        raise MermaidParseError(
            f"invalid timeout duration '{label_value}' "
            "(expected e.g. 'timeout: 30m', '45s', '2h')",
            line_number,
        )
    amount_text, unit_text = timeout_match.groups()
    timeout_seconds = float(amount_text) * _TIMEOUT_UNIT_SECONDS[unit_text]
    if timeout_seconds <= 0:
        raise MermaidParseError(
            f"invalid timeout duration '{label_value}': must be greater than 0 "
            "(a zero timeout fires before the node can make progress)",
            line_number,
        )
    return timeout_seconds
