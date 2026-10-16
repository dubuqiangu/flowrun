"""假 opencode CLI（测试夹具）：读参数、按模式打印假 json 输出。

用法: python fake_opencode.py [--mode MODE] [packet 内容 ...]

模式:
  normal             打印一条运行中事件 + 一条终末 assistant 消息对象后退出码 0
  hang-after-message 打印终末消息对象后挂起 60 秒不退出（考验宽限期 kill）
  silent-hang        不输出任何消息并挂起 60 秒（考验总超时 kill）
  fail-no-message    只打印运行中事件（无完成形态）后退出码 3
"""
import json
import sys
import time

HANG_SECONDS = 60


def emit(output_message: dict) -> None:
    print(json.dumps(output_message, ensure_ascii=False), flush=True)


def running_event_message() -> dict:
    return {"type": "session.updated", "status": "running"}


def final_assistant_message(packet_text: str) -> dict:
    return {
        "id": "msg_fake_opencode_0001",
        "role": "assistant",
        "parts": [{"type": "text", "text": f"processed: {packet_text}"}],
        "cost": 0.05,
        "tokens": {"input": 10, "output": 20},
    }


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
    # Windows 默认管道编码是本地代码页；真实 CLI 输出 UTF-8 JSON，这里对齐
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    argv = sys.argv[1:]
    mode = parse_mode(argv)
    packet_text = parse_packet_text(argv)
    print(f"[fake-opencode] packet received: {packet_text}", file=sys.stderr, flush=True)

    if mode == "normal":
        emit(running_event_message())
        emit(final_assistant_message(packet_text))
        return 0
    if mode == "hang-after-message":
        emit(final_assistant_message(packet_text))
        time.sleep(HANG_SECONDS)
        return 0
    if mode == "silent-hang":
        time.sleep(HANG_SECONDS)
        return 0
    if mode == "fail-no-message":
        emit(running_event_message())
        return 3
    print(f"[fake-opencode] unknown mode: {mode}", file=sys.stderr, flush=True)
    return 64


if __name__ == "__main__":
    sys.exit(main())
