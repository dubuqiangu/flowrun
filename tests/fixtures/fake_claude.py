"""假 Claude Code CLI（测试夹具）：读参数、按模式逐行打印假 stream-json。

用法: python fake_claude.py [--mode MODE] [packet 内容 ...]
packet 由适配层作为最后位置参数注入（__FLOW_PACKET__ 默认投递）。

模式:
  normal            打印 system/assistant/result 三条 stream-json 后退出码 0
  hang-after-result 打印到 result 消息后挂起 60 秒不退出（考验宽限期 kill）
  silent-hang       不输出任何消息并挂起 60 秒（考验总超时 kill）
  fail-no-result    打印两条消息（不含 result）后退出码 3
"""
import json
import sys
import time

FAKE_SESSION_ID = "fake-claude-session-0001"
HANG_SECONDS = 60


def emit(stream_message: dict) -> None:
    print(json.dumps(stream_message, ensure_ascii=False), flush=True)


def system_init_message() -> dict:
    return {"type": "system", "subtype": "init", "session_id": FAKE_SESSION_ID}


def assistant_text_message(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def result_message(packet_text: str) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "session_id": FAKE_SESSION_ID,
        "result": f"processed: {packet_text}",
        "is_error": False,
        "total_cost_usd": 0.42,
        "duration_ms": 1234,
        "num_turns": 2,
        "usage": {"input_tokens": 100, "output_tokens": 200},
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
    # 投递核对证据：把收到的 packet 回显到 stderr（stderr 审计文件可断言）
    print(f"[fake-claude] packet received: {packet_text}", file=sys.stderr, flush=True)

    if mode == "normal":
        emit(system_init_message())
        emit(assistant_text_message("working on it"))
        emit(result_message(packet_text))
        return 0
    if mode == "hang-after-result":
        emit(system_init_message())
        emit(result_message(packet_text))
        time.sleep(HANG_SECONDS)  # 跑完不退，考验监视器宽限期 kill
        return 0
    if mode == "silent-hang":
        time.sleep(HANG_SECONDS)  # 挂死不报，考验总超时 kill
        return 0
    if mode == "fail-no-result":
        emit(system_init_message())
        emit(assistant_text_message("incomplete work"))
        return 3
    print(f"[fake-claude] unknown mode: {mode}", file=sys.stderr, flush=True)
    return 64


if __name__ == "__main__":
    sys.exit(main())
