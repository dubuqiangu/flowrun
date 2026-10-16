"""通用假行流 agent（base 监视器测试夹具）：读参数、按模式打印纯文本行。

用法: python fake_line_agent.py [--mode MODE] [packet 内容 ...]

模式:
  normal        打印 META 行、进度行、DONE 行后退出码 0
  hang-after-done 打印 DONE 后挂起 60 秒不退出（考验宽限期 kill）
  silent-hang   不输出任何消息并挂起 60 秒（考验总超时 kill）
  exit-failure  打印进度行（无 DONE）后退出码 2
"""
import sys
import time

HANG_SECONDS = 60


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


def main() -> int:
    # Windows 默认管道编码是本地代码页；对齐真实 CLI 的 UTF-8 输出
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    argv = sys.argv[1:]
    mode = parse_mode(argv)
    packet_text = parse_packet_text(argv)
    print(f"[fake-line-agent] packet received: {packet_text}", file=sys.stderr, flush=True)

    if mode == "normal":
        print("META answer=42", flush=True)
        print("progress tick", flush=True)
        print("DONE", flush=True)
        return 0
    if mode == "hang-after-done":
        print("DONE", flush=True)
        time.sleep(HANG_SECONDS)
        return 0
    if mode == "silent-hang":
        time.sleep(HANG_SECONDS)
        return 0
    if mode == "exit-failure":
        print("progress tick", flush=True)
        return 2
    print(f"[fake-line-agent] unknown mode: {mode}", file=sys.stderr, flush=True)
    return 64


if __name__ == "__main__":
    sys.exit(main())
