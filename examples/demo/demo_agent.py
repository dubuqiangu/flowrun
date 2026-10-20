"""样例项目的假 agent：按 packet 里的产物契约交付文件，并输出 usage。

为什么是假 agent：样例的目的在于演示 FlowRun 的**流程控制**（编译 / 调度 / 产物核对 /
判定 / 人审 / 闸 / 恢复），而不是演示某个真实模型的产出质量。本脚本不联网、不调用
任何 LLM，行为完全确定——这样跑两次看到的结果一模一样，才便于对照账本。

用法：python demo_agent.py [--mode MODE] <packet 文本>

模式（由 frontmatter 的 cmd 传入）：
  normal  按 packet 的 "contract: [a, b]" 逐项创建产物，自报后打印一行 usage JSON
  lie     自称做完、退出码 0，但**一个产物都不写**——演示产物核对如何拦下这种叙事
  crash   不写产物并异常退出（退出码 3）——演示进程级失败分支
"""

import argparse
import json
import sys
from pathlib import Path

PRODUCED_MARKER = "produced by flowrun demo agent"
FAIL_EXIT_CODE = 3
DEMO_USAGE_JSON = {"usage": {"input_tokens": 120, "output_tokens": 180}}


def parse_contract_paths(packet_text: str) -> list[str]:
    """只做结构化解析：找 "contract: [a, b]" 行，不执行任何内容。"""
    for raw_line in packet_text.splitlines():
        stripped_line = raw_line.strip()
        if not stripped_line.startswith("contract:"):
            continue
        bracket_content = stripped_line[len("contract:"):].strip().strip("[]")
        return [item.strip() for item in bracket_content.split(",") if item.strip()]
    return []


def deliver_products(contract_paths: list[str]) -> None:
    """逐项创建产物（含父目录），内容是固定哨兵——可复核、可重复。"""
    for contract_path in contract_paths:
        product_file = Path(contract_path)
        product_file.parent.mkdir(parents=True, exist_ok=True)
        product_file.write_text(PRODUCED_MARKER + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    # 对齐真实 headless CLI：stdout 固定 UTF-8，避免 Windows 本地代码页污码
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="FlowRun 样例假 agent")
    parser.add_argument("--mode", default="normal")
    # packet 正文由适配层作为最后一个位置参数注入，可能含空行/空格，故用 parse_known_args
    arguments, packet_parts = parser.parse_known_args(argv)

    if arguments.mode == "lie":
        # 自称做完、退出码 0，却一个产物都没写——产物核对会把它拦下来。
        # 这是最常见也最危险的失败形态：agent 的"叙事"与"交付"不一致。
        print("自报：任务已完成，所有产物均已写入。", flush=True)
        print(json.dumps(DEMO_USAGE_JSON, ensure_ascii=False), flush=True)
        return 0

    if arguments.mode == "crash":
        # 直接异常退出：演示进程级失败（reason=nonzero_exit）的分支。
        print("自报：执行中断。", file=sys.stderr, flush=True)
        return FAIL_EXIT_CODE

    packet_text = " ".join(packet_parts)
    contract_paths = parse_contract_paths(packet_text)
    deliver_products(contract_paths)
    print(f"自报：已修改 {len(contract_paths)} 个文件：{contract_paths}", flush=True)
    print(json.dumps(DEMO_USAGE_JSON, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))