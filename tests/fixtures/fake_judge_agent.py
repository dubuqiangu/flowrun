"""假判定 agent（kernel judge 测试夹具）：读 packet，按模式回显指定 JSON。

用法: python fake_judge_agent.py --mode MODE [packet 内容 ...]

packet 由适配层默认投递方式追加为最后一个位置参数（`claude -p "$(cat
packet.md)"` 语义），与真实 headless CLI 一致。

模式:
  hit                          从 packet 提取第一条候选边编号，回显命中 verdict
  miss                         回显一个不在候选集合内的边编号
  low-confidence               命中候选边但 confidence=0.3（低于门槛）
  no-json                      只打印纯文本，不输出任何 JSON
  two-json                     先回显一个 miss JSON + 一段散文，再回显 hit JSON
                               （考验"提取最后一个 JSON 对象"）
  flaky-miss-then-hit          第 1 次投递回显 miss，之后回显 hit（考验重试）
  flaky-low-confidence-then-hit 第 1 次投递回显 low-confidence，之后 hit

flaky 模式用 cwd 下的 .fake_judge_agent_visits 计数文件跨进程记录投递次数
（适配器以 work_dir 为 cwd 拉起本脚本，重试即第二次进程）。
"""
import json
import re
import sys
from pathlib import Path

VISIT_COUNTER_FILENAME = ".fake_judge_agent_visits"
CANDIDATE_EDGE_PATTERN = re.compile(r"^\s*\d+\.\s+(\S+->\S+)", re.MULTILINE)


def parse_mode(argv: list[str]) -> str:
    mode = "hit"
    for argument_index, argument in enumerate(argv):
        if argument == "--mode" and argument_index + 1 < len(argv):
            mode = argv[argument_index + 1]
    return mode


def parse_packet_text(argv: list[str]) -> str:
    packet_parts = [
        argument
        for index, argument in enumerate(argv)
        if index > 0 and argv[index - 1] != "--mode"
    ]
    return " ".join(packet_parts)


def first_candidate_edge_number(packet_text: str) -> str:
    """从判定 packet 提取第一条候选边编号（packet 正文 "1. D001->H001" 行）。"""
    candidate_match = CANDIDATE_EDGE_PATTERN.search(packet_text)
    if candidate_match is None:
        return "NO-CANDIDATE->NO-CANDIDATE"
    return candidate_match.group(1)


def count_visits() -> int:
    """读 cwd 计数文件并 +1 写回，返回本次是第几次投递（首次为 1）。"""
    visit_counter_path = Path.cwd() / VISIT_COUNTER_FILENAME
    try:
        visit_count = int(visit_counter_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        visit_count = 0
    visit_count += 1
    visit_counter_path.write_text(str(visit_count), encoding="utf-8")
    return visit_count


def emit_verdict(edge_number: str, confidence: float) -> None:
    print(
        json.dumps(
            {
                "edge": edge_number,
                "confidence": confidence,
                "evidence": ["产物契约齐全", "退出码为 0"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def main() -> int:
    # Windows 默认管道编码是本地代码页；对齐真实 CLI 的 UTF-8 输出
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    argv = sys.argv[1:]
    mode = parse_mode(argv)
    packet_text = parse_packet_text(argv)
    hit_edge_number = first_candidate_edge_number(packet_text)
    print(f"[fake-judge-agent] packet received ({len(packet_text)} chars)", file=sys.stderr, flush=True)

    if mode == "hit":
        emit_verdict(hit_edge_number, 0.9)
        return 0
    if mode == "miss":
        emit_verdict("BOGUS->NOWHERE", 0.9)
        return 0
    if mode == "low-confidence":
        emit_verdict(hit_edge_number, 0.3)
        return 0
    if mode == "no-json":
        print("我认为整体完成度不错，但没有可以结构化的结论。", flush=True)
        return 0
    if mode == "two-json":
        # 先给一个错误 JSON + 散文，再给正确 JSON：judge 必须取最后一个
        emit_verdict("BOGUS->EARLIER", 0.95)
        print("上面是初步想法，下面是最终结论：", flush=True)
        emit_verdict(hit_edge_number, 0.85)
        return 0
    if mode == "flaky-miss-then-hit":
        if count_visits() == 1:
            emit_verdict("BOGUS->FIRST-ATTEMPT", 0.9)
        else:
            emit_verdict(hit_edge_number, 0.9)
        return 0
    if mode == "flaky-low-confidence-then-hit":
        if count_visits() == 1:
            emit_verdict(hit_edge_number, 0.3)
        else:
            emit_verdict(hit_edge_number, 0.9)
        return 0
    print(f"[fake-judge-agent] unknown mode: {mode}", file=sys.stderr, flush=True)
    return 64


if __name__ == "__main__":
    sys.exit(main())
