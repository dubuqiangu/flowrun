"""FIX-2 测试夹具：睡满指定秒数后写 marker 文件。

模拟"垫片 .cmd 拉起的孙进程"——它不在直接子进程上，只有连坐进程树
（taskkill /T）才能被终止；若只杀父进程，它会在超时后继续写 workspace。
用法：``python slow_marker_writer.py <delay_seconds> <marker_path>``
"""

import sys
import time
from pathlib import Path


def main() -> int:
    delay_seconds = float(sys.argv[1])
    marker_path = Path(sys.argv[2])
    time.sleep(delay_seconds)
    marker_path.write_text("grandchild-finished", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
