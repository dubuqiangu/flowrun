"""域D2 · usage token 记账单元测试：extract_usage_tokens 不得重复计数。

直跑：python -m unittest discover -s tests
"""

import asyncio
import tempfile
import unittest
from pathlib import Path

import sys

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))

import kernel_test_support as support  # noqa: E402

from flowrun.adapters.base import DispatchResult  # noqa: E402
from flowrun.kernel.engine import run_flow  # noqa: E402
from flowrun.kernel.engine_evidence import extract_usage_tokens  # noqa: E402
from flowrun.state.ledger import EventLedger  # noqa: E402


class ExtractUsageTokensTestCase(unittest.TestCase):
    """usage dict 含 total 时优先取 total，不把总数与分量相加。"""

    def test_missing_usage_returns_zero(self) -> None:
        self.assertEqual(extract_usage_tokens({}), 0)

    def test_single_number_usage_is_used_directly(self) -> None:
        self.assertEqual(extract_usage_tokens({"usage": 42}), 42)

    def test_bool_usage_is_not_counted(self) -> None:
        self.assertEqual(extract_usage_tokens({"usage": True}), 0)

    def test_component_only_usage_is_summed(self) -> None:
        self.assertEqual(
            extract_usage_tokens({"usage": {"input_tokens": 10, "output_tokens": 20}}),
            30,
        )

    def test_total_tokens_wins_over_components(self) -> None:
        self.assertEqual(
            extract_usage_tokens(
                {"usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}}
            ),
            30,
            "总数已含分量，不得累加成 60",
        )

    def test_total_key_wins_over_components(self) -> None:
        self.assertEqual(
            extract_usage_tokens(
                {"usage": {"input_tokens": 10, "output_tokens": 20, "total": 30}}
            ),
            30,
        )

    def test_non_numeric_total_tokens_is_not_summed_as_string(self) -> None:
        self.assertEqual(extract_usage_tokens({"usage": {"total_tokens": "30"}}), 0)

    def test_top_level_total_tokens_used_when_usage_absent(self) -> None:
        self.assertEqual(extract_usage_tokens({"total_tokens": 77}), 77)

    def test_bool_leaf_is_not_counted_as_one(self) -> None:
        self.assertEqual(
            extract_usage_tokens({"usage": {"input_tokens": 5, "flag": True}}), 5
        )


class UsageAccountingEndToEndTestCase(unittest.TestCase):
    """dispatch usage 含总数与分量时，账本 payload.tokens 只记总数。"""

    def test_agent_exited_tokens_uses_total_not_sum(self) -> None:
        with tempfile.TemporaryDirectory(prefix="flowrun_usage_test_") as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            stubs = support.KernelHookStubs()
            task_node = support.make_node("T001")
            flow_ir = support.make_flow_ir([task_node], [])
            stubs.script_dispatch_result(
                "T001",
                DispatchResult(
                    exit_code=0,
                    timed_out=False,
                    semantic_done=False,
                    stdout_path=Path("audit") / "stub-stdout.log",
                    duration_seconds=0.01,
                    structured_result={
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 200,
                            "total_tokens": 300,
                        }
                    },
                ),
            )
            final_state = asyncio.run(
                run_flow(flow_ir, workspace, stubs.build_hooks())
            )
            self.assertTrue(final_state.is_done())
            ledger = EventLedger(support.run_dir_for(workspace) / "ledger.jsonl")
            exited_events = support.events_of_type(ledger, "agent_exited", "T001")
            self.assertEqual(len(exited_events), 1)
            self.assertEqual(
                exited_events[0].payload["tokens"], 300, "总数与分量不得重复累加"
            )
            self.assertEqual(final_state.token_spent(), 300)


if __name__ == "__main__":
    unittest.main()
