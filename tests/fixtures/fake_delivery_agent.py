"""假交付 agent（T013 冒烟夹具）：写出 out= 产物并在 stdout 自报。

用法: python fake_delivery_agent.py [--mode MODE] [packet 内容 ...]
packet 由适配层默认投递方式追加为最后一个位置参数（与生产 headless CLI 一致）。

模式:
  normal   从 packet 解析 "contract: [a, b]"，逐个创建产物文件（哨兵内容），
           先打印自报文本（改了哪些文件、结果如何），再打印一条 usage JSON
           行，退出码 0 —— 自报在前、JSON 在后，模拟真实 agent 的叙事输出
  fail     打印自报（未交付任何产物）后退出码 3，不创建产物（失败注入）
"""
import json
import sys
from pathlib import Path

OUTPUT_SENTINEL_CONTENT = "produced by fake_delivery_agent"
FAIL_EXIT_CODE = 3


def parse_mode(argv: list[str]) -> str:
    mode = "normal"
    for argument_index, argument in enumerate(argv):
        if argument == "--mode" and argument_index + 1 < len(argv):
            mode = argv[argument_index + 1]
    return mode


def parse_packet_text(argv: list[str]) -> str:
    """取出全部非 --mode 参数并拼回 packet 文本。

    packet 由适配层默认投递方式追加为最后一个位置参数；无 --mode 时它就是
    argv[0]，故不能用"跳过首参数"的取法——按参数对剔除 --mode 及其值即可。
    """
    packet_parts = []
    skip_next_argument = False
    for argument in argv:
        if skip_next_argument:
            skip_next_argument = False
            continue
        if argument == "--mode":
            skip_next_argument = True
            continue
        packet_parts.append(argument)
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


def emit_self_report(delivered_paths: list[str]) -> None:
    if delivered_paths:
        print(
            "自报：本次修改了 " + "、".join(delivered_paths)
            + "；结果：全部测试通过，产物已交付。",
            flush=True,
        )
    else:
        print("自报：本次未交付任何产物；结果：执行失败。", flush=True)


def emit_usage_json() -> None:
    print(
        json.dumps(
            {"usage": {"input_tokens": 120, "output_tokens": 180}},
            ensure_ascii=False,
        ),
        flush=True,
    )


def main() -> int:
    # Windows 默认管道编码是本地代码页；对齐真实 CLI 的 UTF-8 JSON 输出
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    argv = sys.argv[1:]
    mode = parse_mode(argv)
    packet_text = parse_packet_text(argv)

    if mode == "normal":
        contract_paths = parse_contract_paths(packet_text)
        for contract_path in contract_paths:
            target_file = Path(contract_path)
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text(OUTPUT_SENTINEL_CONTENT, encoding="utf-8")
        emit_self_report(contract_paths)
        emit_usage_json()
        return 0
    if mode == "fail":
        emit_self_report([])
        return FAIL_EXIT_CODE
    print(f"[fake_delivery_agent] unknown mode: {mode}", file=sys.stderr, flush=True)
    return 64


if __name__ == "__main__":
    sys.exit(main())
