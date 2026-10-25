"""域D2 内核闸测试共享支撑：临时 workspace、单次 run_flow、重开账本读取。

被 test_kernel_engine_gates.py / test_kernel_engine_gates_recovery.py /
test_kernel_engine_terminal_state_guards.py 复用（文件名不带 test_ 前缀，
避免被 unittest 收集）。
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import kernel_test_support

import kernel_test_support as support  # noqa: E402  (负责把 src/ 加进 sys.path)
from flowrun.kernel.engine import run_flow  # noqa: E402
from flowrun.state.ledger import EventLedger  # noqa: E402


class KernelGatesHarnessTestCase(unittest.TestCase):
    """跑一次 run_flow 的最小夹具：临时 workspace + 可编程 hook stub。"""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_gates_test_")
        # 注册为 cleanup（LIFO）：测试期间注册的账本 close 先执行，之后才删临时目录；
        # 若在 tearDown 里删目录，Windows 上仍打开的 ledger.jsonl 会让 rmtree 失败。
        self.addCleanup(self._temp_dir.cleanup)
        self.workspace = Path(self._temp_dir.name) / "workspace"
        self.stubs = support.KernelHookStubs()

    def run_dir(self) -> Path:
        """本次测试 run 的实际目录（T022：namespaced 布局在 <ws>/<run>/）。"""
        return support.run_dir_for(self.workspace, support.DEFAULT_RUN_NAME)

    def run_flow_once(self, flow_ir) -> object:
        return asyncio.run(run_flow(flow_ir, self.workspace, self.stubs.build_hooks()))

    def reopened_ledger(self) -> EventLedger:
        """重开账本读取落账结果；注册清理，避免测试结束后遗留写句柄（ResourceWarning）。"""
        ledger = EventLedger(self.run_dir() / "ledger.jsonl")
        self.addCleanup(ledger.close)
        return ledger
