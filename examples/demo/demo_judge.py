"""样例项目的假判定 agent：读证据包，回一个 JSON verdict。

判定协议由 FlowRun 定义（design.md §5.2）：判定 agent 只需在 stdout 打印一个
严格 JSON 对象，字段 edge（候选边编号，形如 "D001->H001"）与 confidence（0~1）。
本脚本不判断任何真实语义——它按 --mode 固定回显，让"判定走哪条边"在你实测时可预期。

用法：python demo_judge.py [--mode hit|miss] <packet 文本>

  hit   从 packet 的候选边清单里取第一条，confidence=0.95（判定通过）
  miss  回一个不在候选集合的边编号（触发重试 → 仍失败则落 verdict 闸转人审）
"""

import argparse
import json
import re
import sys

CANDIDATE_EDGE_PATTERN = re.compile(r"^\s*\d+\.\s+(\S+->\S+)", re.MULTILINE)
DEFAULT_CONFIDENCE = 0.95
MISS_EDGE_NUMBER = "D901->T901"


def first_candidate_edge_number(packet_text: str) -> str | None:
    """从 packet 的候选项列表里取第一条边编号（只解析文本，不执行）。"""
    matched_edge = CANDIDATE_EDGE_PATTERN.search(packet_text)
    return matched_edge.group(1) if matched_edge is not None else None


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="FlowRun 样例假判定 agent")
    parser.add_argument("--mode", default="hit")
    arguments, packet_parts = parser.parse_known_args(argv)
    packet_text = " ".join(packet_parts)

    edge_number = (
        MISS_EDGE_NUMBER
        if arguments.mode == "miss"
        else first_candidate_edge_number(packet_text)
    )
    if edge_number is None:
        print("找不到候选边清单，无法给出 verdict", file=sys.stderr, flush=True)
        return 2

    print("判定说明：上游产物契约已逐项核对。", flush=True)
    print(
        json.dumps(
            {
                "edge": edge_number,
                "confidence": DEFAULT_CONFIDENCE,
                "evidence": ["上游产物齐全"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))