"""假流程 agent（测试夹具）：模拟一个会兑现产物契约的 agent CLI。

用法: python fake_flow_agent.py [--mode MODE] [packet 内容 ...]
packet 由内核 hook 作为最后位置参数注入（与生产适配器的默认投递方式一致）。

模式:
  normal   从 packet 文本解析 "contract: [a, b]" 行，逐个创建产物文件
           （内容为哨兵文本），打印一条含 usage 的 JSON 结果行，退出码 0
  fail     打印 usage 后退出码 3，不创建任何产物（考验失败路径）
"""
import json
import sys
from pathlib import Path

OUTPUT_SENTINEL_CONTENT = "produced by fake_flow_agent"
FAIL_EXIT_CODE = 3


def parse_mode(argv: list[str]) -> str:
    mode = "normal"
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


def parse_contract_paths(packet_text: str) -> list[str]:
    """只做结构化解析：找 "contract: [a, b]" 行并提取清单（绝不执行内容）。"""
    for raw_line in packet_text.splitlines():
        stripped_line = raw_line.strip()
        if stripped_line.startswith("contract:"):
            bracket_content = stripped_line[len("contract:"):].strip().strip("[]")
            return [
                item.strip()
                for item in bracket_content.split(",")
                if item.strip()
            ]
    return []


def main() -> int:
    # Windows 默认管道编码是本地代码页；这里对齐真实 CLI 的 UTF-8 JSON 输出
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    argv = sys.argv[1:]
    mode = parse_mode(argv)
    packet_text = parse_packet_text(argv)

    if mode == "normal":
        for contract_path in parse_contract_paths(packet_text):
            target_file = Path(contract_path)
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text(OUTPUT_SENTINEL_CONTENT, encoding="utf-8")
        print(
            json.dumps(
                {"usage": {"input_tokens": 100, "output_tokens": 200}},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0
    if mode == "fail":
        print(
            json.dumps({"usage": {"input_tokens": 50, "output_tokens": 50}}, ensure_ascii=False),
            flush=True,
        )
        return FAIL_EXIT_CODE
    print(f"[fake_flow_agent] unknown mode: {mode}", file=sys.stderr, flush=True)
    return 64


if __name__ == "__main__":
    sys.exit(main())
