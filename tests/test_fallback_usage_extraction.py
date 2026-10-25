"""T020f · 兜底适配器（长尾 CLI）的用量提取测试。

背景：未注册命令头的长尾 CLI 走 ``FallbackAdapter``，修复前 ``parse_line`` 恒不产
``structured_result`` → task 路径的 ``agent_exited`` 里没有 ``tokens``，budget 闸对
长尾 CLI 的真实花费完全失明（计量错误：不是"少记一笔"，是把真实消耗记成 0）。

钉三件事：
1. **约定识别**——"一行里最后一个 JSON 对象，且含 ``usage`` / ``total_tokens``
   顶层键"是 flowrun 自己声明的约定，不依赖任何 agent 内部 API，也不解析任何厂商
   文案；不含这两个键的行一律不产结构化字段；
2. **口径一致**——搬出的结构化结果喂给 ``extract_usage_tokens`` 后得数与判定/任务
   路径完全相同（总数键优先、分量不重复累加）；
3. **不动既有语义**——``semantic_done`` 恒 False（兜底适配器只认"退出码 + 超时"），
   stdout 审计落盘不受影响；用量声明不得改变完成判定。

直跑：python -m unittest discover -s tests
"""

import asyncio
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import kernel_test_support

import kernel_test_support as support  # noqa: E402  (负责把 src/ 加进 sys.path)

from flowrun.kernel.engine import run_flow  # noqa: E402
from flowrun.kernel.engine_evidence import extract_usage_tokens  # noqa: E402
from flowrun.kernel.fallback_adapter import FallbackAdapter  # noqa: E402
from flowrun.state.ledger import EventLedger  # noqa: E402

PACKET_TEXT = "实现登录 API：JWT 签发与校验、bcrypt 存储。"
FAKE_LONGTAIL_SCRIPT_NAME = "fake_longtail_cli.py"


def write_stdout_script(script_path: Path, stdout_lines: list[str], exit_code: int) -> None:
    """写一个"只按序打印给定行然后退出"的最小假 CLI（长尾 CLI 的替身）。"""
    script_path.write_text(
        "import sys\n"
        'sys.stdout.reconfigure(encoding="utf-8")\n'
        f"for output_line in {stdout_lines!r}:\n"
        "    print(output_line, flush=True)\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )


@contextmanager
def fallback_dispatch_scope(stdout_lines: list[str], exit_code: int = 0,
                            timeout_seconds: float = 30.0):
    """在 work_dir 存活期内产出 DispatchResult（审计文件断言须在此作用域内做）。"""
    adapter = FallbackAdapter()
    with tempfile.TemporaryDirectory(prefix="flowrun_fallback_usage_") as temp_dir_name:
        work_dir = Path(temp_dir_name)
        script_path = work_dir / FAKE_LONGTAIL_SCRIPT_NAME
        write_stdout_script(script_path, stdout_lines, exit_code)
        packet_path = work_dir / "packet.md"
        packet_path.write_text(PACKET_TEXT, encoding="utf-8")
        yield asyncio.run(
            adapter.dispatch(
                packet_path, [sys.executable, str(script_path)], work_dir, timeout_seconds
            )
        )


class FallbackUsageConventionTestCase(unittest.TestCase):
    """约定识别：哪一行、哪个对象、哪些键才算用量声明。"""

    def setUp(self) -> None:
        self.adapter = FallbackAdapter()

    def test_plain_text_line_declares_no_usage(self) -> None:
        signal_update = self.adapter.parse_line("正在分析代码库……")
        self.assertEqual(signal_update.structured_fields, {})
        self.assertFalse(signal_update.semantic_done)

    def test_json_without_usage_keys_declares_no_usage(self) -> None:
        signal_update = self.adapter.parse_line('{"status": "working", "step": 3}')
        self.assertEqual(signal_update.structured_fields, {})

    def test_usage_dict_is_carried_verbatim(self) -> None:
        signal_update = self.adapter.parse_line(
            '{"usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}}'
        )
        self.assertEqual(
            signal_update.structured_fields,
            {"usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}},
        )
        self.assertFalse(signal_update.semantic_done, "用量声明不得置位语义完成信号")

    def test_top_level_total_tokens_is_carried_verbatim(self) -> None:
        signal_update = self.adapter.parse_line('{"total_tokens": 77}')
        self.assertEqual(signal_update.structured_fields, {"total_tokens": 77})

    def test_single_number_usage_is_carried(self) -> None:
        signal_update = self.adapter.parse_line('{"usage": 42}')
        self.assertEqual(signal_update.structured_fields, {"usage": 42})

    def test_bool_and_string_usage_values_are_not_carried(self) -> None:
        self.assertEqual(self.adapter.parse_line('{"usage": true}').structured_fields, {})
        self.assertEqual(
            self.adapter.parse_line('{"usage": "300"}').structured_fields, {}
        )

    def test_json_object_after_log_prefix_is_recognized(self) -> None:
        signal_update = self.adapter.parse_line('[tool] done {"usage": {"total_tokens": 12}}')
        self.assertEqual(signal_update.structured_fields, {"usage": {"total_tokens": 12}})

    def test_braces_inside_json_string_do_not_break_detection(self) -> None:
        signal_update = self.adapter.parse_line(
            '{"note": "a } b { c", "usage": {"total_tokens": 5}}'
        )
        self.assertEqual(signal_update.structured_fields, {"usage": {"total_tokens": 5}})

    def test_last_json_object_in_line_decides(self) -> None:
        """一行里两个对象：只认最后一个（前一个是早期草稿声明）。"""
        signal_update = self.adapter.parse_line(
            '{"usage": {"total_tokens": 111}} {"usage": {"total_tokens": 222}}'
        )
        self.assertEqual(signal_update.structured_fields, {"usage": {"total_tokens": 222}})

    def test_last_json_object_without_usage_declares_nothing(self) -> None:
        """末个对象不含用量键 → 本行不算用量声明（前一个对象不作数）。"""
        signal_update = self.adapter.parse_line(
            '{"usage": {"total_tokens": 111}} {"status": "done"}'
        )
        self.assertEqual(signal_update.structured_fields, {})

    def test_extracted_fields_use_extract_usage_tokens_semantics(self) -> None:
        """总数键优先、分量不重复累加——与判定/任务路径同一口径。"""
        signal_update = self.adapter.parse_line(
            '{"usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}}'
        )
        self.assertEqual(
            extract_usage_tokens(signal_update.structured_fields), 30, "不得累加成 60"
        )


class FallbackUsageDispatchTestCase(unittest.TestCase):
    """投递级：结构化结果、审计落盘与语义完成信号三者的关系。"""

    def test_usage_line_lands_in_structured_result(self) -> None:
        with fallback_dispatch_scope(
            ["正在分析……", '{"usage": {"input_tokens": 10, "output_tokens": 20}}']
        ) as result:
            self.assertEqual(result.exit_code, 0)
            self.assertFalse(result.semantic_done, "兜底适配器恒不产语义完成信号")
            self.assertFalse(result.timed_out)
            self.assertEqual(
                result.structured_result, {"usage": {"input_tokens": 10, "output_tokens": 20}}
            )
            self.assertEqual(extract_usage_tokens(result.structured_result), 30)
            # 原始输出仍全量落盘审计（用量提取不得动审计通道）
            stdout_audit = result.stdout_path.read_text(encoding="utf-8")
            self.assertIn('"usage"', stdout_audit)
            self.assertIn("正在分析", stdout_audit)

    def test_last_usage_line_wins_across_stream(self) -> None:
        with fallback_dispatch_scope(
            [
                '{"usage": {"total_tokens": 11}}',
                "中间还有别的日志",
                '{"usage": {"total_tokens": 22}}',
            ]
        ) as result:
            self.assertEqual(extract_usage_tokens(result.structured_result), 22)

    def test_cli_without_usage_declaration_keeps_structured_result_empty(self) -> None:
        with fallback_dispatch_scope(["普通日志一行", '{"status": "ok"}']) as result:
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.structured_result, {}, "无声明不得凭空造用量")
            self.assertFalse(result.semantic_done)

    def test_nonzero_exit_with_usage_still_reports_usage_and_no_semantic_done(self) -> None:
        """非零退出但声明了用量：用量照样入账，完成判定仍只由退出码/超时决定。"""
        with fallback_dispatch_scope(
            ['{"usage": {"total_tokens": 40}}'], exit_code=3
        ) as result:
            self.assertEqual(result.exit_code, 3)
            self.assertFalse(result.semantic_done)
            self.assertEqual(extract_usage_tokens(result.structured_result), 40)


class FallbackDispatchStubs(support.KernelHookStubs):
    """把 task 节点的投递改走真实 ``FallbackAdapter``（真子进程 + 审计落盘）。"""

    def __init__(self, stdout_lines: list[str], exit_code: int = 0) -> None:
        super().__init__()
        self._stdout_lines = list(stdout_lines)
        self._exit_code = exit_code

    async def dispatch_task(self, node, packet_path, timeout_seconds):
        self.dispatch_calls.append((node.node_id, timeout_seconds))
        packet_path = Path(packet_path)
        workspace = packet_path.parent.parent
        script_path = workspace / FAKE_LONGTAIL_SCRIPT_NAME
        write_stdout_script(script_path, self._stdout_lines, self._exit_code)
        return await FallbackAdapter().dispatch(
            packet_path, [sys.executable, str(script_path)], workspace, timeout_seconds
        )


class FallbackUsageEngineAccountingTestCase(unittest.TestCase):
    """端到端：长尾 CLI 的声明用量必须走到 ``agent_exited.tokens`` 与 token 账。"""

    USAGE_LINE = '{"usage": {"input_tokens": 100, "output_tokens": 200, "total_tokens": 300}}'

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="flowrun_fallback_usage_e2e_")
        self.addCleanup(self._temp_dir.cleanup)
        self.workspace = Path(self._temp_dir.name) / "workspace"

    def run_dir(self) -> Path:
        """本次测试 run 的实际目录（T022：namespaced 布局在 <ws>/<run>/）。"""
        return support.run_dir_for(self.workspace, support.DEFAULT_RUN_NAME)

    def run_flow_with_longtail_agent(self, budget_tokens: int = support.DEFAULT_BUDGET_TOKENS):
        task_node = support.make_node("T001", label="长尾 CLI 任务")
        flow_ir = support.make_flow_ir([task_node], [], budget_tokens=budget_tokens)
        stubs = FallbackDispatchStubs([self.USAGE_LINE])
        return asyncio.run(run_flow(flow_ir, self.workspace, stubs.build_hooks())), flow_ir

    def reopened_ledger(self) -> EventLedger:
        ledger = EventLedger(self.run_dir() / "ledger.jsonl")
        self.addCleanup(ledger.close)
        return ledger

    def test_longtail_usage_reaches_agent_exited_and_token_spent(self) -> None:
        final_state, _flow_ir = self.run_flow_with_longtail_agent()

        self.assertTrue(final_state.is_done())
        exited_events = support.events_of_type(self.reopened_ledger(), "agent_exited", "T001")
        self.assertEqual(len(exited_events), 1)
        self.assertEqual(exited_events[0].payload["tokens"], 300, "总量已含分量，不得累加")
        self.assertFalse(exited_events[0].payload["semantic_done"])
        self.assertEqual(final_state.token_spent(), 300)

    def test_longtail_usage_can_trip_budget_gate(self) -> None:
        """修复前 tokens 恒 0 → budget 闸看不见长尾 CLI 的花费（计量错误）。"""
        final_state, _flow_ir = self.run_flow_with_longtail_agent(budget_tokens=100)

        budget_gate_entries = [
            entry
            for entry in support.events_of_type(self.reopened_ledger(), "gate_tripped")
            if entry.payload.get("gate") == "budget"
        ]
        self.assertEqual(len(budget_gate_entries), 1, "长尾 CLI 的花费必须能被 budget 闸看到")
        self.assertEqual(budget_gate_entries[0].payload["actual"], 300)
        self.assertFalse(final_state.is_done(), "预算超限不得收口")


if __name__ == "__main__":
    unittest.main()
