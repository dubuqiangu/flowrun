"""kernel judge 纯函数测试（域D1 · T009）：谓词边判定 is_predicate_edge、stdout 中
最后一个可解析 JSON 判定的提取 extract_last_verdict（含 K4：字符串上下文内的花括号
不得破坏平衡扫描）。

判定协议整体（子进程 + 账本）见 test_kernel_judge.py。

直跑：python -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from flowrun.ir.schema import EdgeSpec  # noqa: E402
from flowrun.kernel.judge import (  # noqa: E402
    extract_last_verdict,
    is_predicate_edge,
)


class IsPredicateEdgeTest(unittest.TestCase):
    def test_natural_language_when_is_predicate(self):
        self.assertTrue(is_predicate_edge(EdgeSpec("D001", "H001", "ok", 0, None, 1)))

    def test_unconditional_edge_is_not_predicate(self):
        self.assertFalse(is_predicate_edge(EdgeSpec("T001", "D001", None, 0, None, 1)))

    def test_barrier_edge_is_not_predicate(self):
        self.assertFalse(is_predicate_edge(EdgeSpec("J001", "D001", "all", 0, None, 1)))

    def test_timeout_edge_is_not_predicate(self):
        self.assertFalse(is_predicate_edge(EdgeSpec("D001", "T003", None, 0, 1800.0, 1)))

    def test_malformed_when_with_timeout_is_not_predicate(self):
        self.assertFalse(is_predicate_edge(EdgeSpec("D001", "T003", "ok", 0, 1800.0, 1)))


class ExtractLastVerdictTest(unittest.TestCase):
    def test_takes_the_last_parseable_json_object(self):
        stdout_text = (
            '思考中...\n{"edge":"BOGUS->EARLIER","confidence":0.95,"evidence":[]}\n'
            '结论如下\n{"edge":"D001->H001","confidence":0.8,"evidence":["契约齐全"]}\n'
        )
        verdict = extract_last_verdict(stdout_text)
        self.assertEqual(verdict["edge"], "D001->H001")
        self.assertEqual(verdict["confidence"], 0.8)
        self.assertEqual(verdict["evidence"], ["契约齐全"])

    def test_returns_none_for_plain_text(self):
        self.assertIsNone(extract_last_verdict("没有任何结构化输出。\n"))

    def test_rejects_non_numeric_confidence(self):
        self.assertIsNone(
            extract_last_verdict('{"edge":"D001->H001","confidence":"high"}')
        )

    def test_rejects_boolean_confidence(self):
        self.assertIsNone(
            extract_last_verdict('{"edge":"D001->H001","confidence":true}')
        )

    def test_missing_evidence_defaults_to_empty_list(self):
        verdict = extract_last_verdict('{"edge":"D001->H001","confidence":0.7}')
        self.assertEqual(verdict["evidence"], [])

    # ---- K4：字符串上下文内的花括号不得破坏平衡扫描

    def test_closing_brace_inside_string_does_not_break_extraction(self):
        stdout_text = '{"edge":"D001->H001","confidence":0.9,"evidence":["a } b"]}'
        verdict = extract_last_verdict(stdout_text)
        self.assertIsNotNone(verdict, "字符串内的 } 是合法 JSON，必须仍能提取判定")
        self.assertEqual(verdict["edge"], "D001->H001")
        self.assertEqual(verdict["evidence"], ["a } b"])

    def test_opening_brace_inside_string_does_not_break_extraction(self):
        stdout_text = '结论\n{"edge":"D001->H001","confidence":0.9,"evidence":["a { b"]}\n'
        verdict = extract_last_verdict(stdout_text)
        self.assertIsNotNone(verdict, "字符串内的 { 是合法 JSON，必须仍能提取判定")
        self.assertEqual(verdict["edge"], "D001->H001")
        self.assertEqual(verdict["evidence"], ["a { b"])

    def test_escaped_quote_inside_string_does_not_break_extraction(self):
        stdout_text = '{"edge":"D001->H001","confidence":0.9,"evidence":["say \\"{\\""]}'
        verdict = extract_last_verdict(stdout_text)
        self.assertIsNotNone(verdict, "转义引号后的花括号仍在字符串内，不得被当作结构括号")
        self.assertEqual(verdict["edge"], "D001->H001")
        self.assertEqual(verdict["evidence"], ['say "{"'])

    def test_nested_object_is_extracted_as_one_candidate(self):
        stdout_text = (
            '{"edge":"D001->H001","confidence":0.9,"evidence":[],'
            '"extra":{"nested":{"deep":true}}}'
        )
        verdict = extract_last_verdict(stdout_text)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["edge"], "D001->H001")

    def test_last_object_wins_when_earlier_object_has_brace_in_string(self):
        stdout_text = (
            '{"edge":"OLD->OLD","confidence":0.9,"evidence":["}"]}\n'
            '{"edge":"D001->H001","confidence":0.8,"evidence":[]}\n'
        )
        verdict = extract_last_verdict(stdout_text)
        self.assertEqual(verdict["edge"], "D001->H001")
        self.assertEqual(verdict["confidence"], 0.8)


if __name__ == "__main__":
    unittest.main()
