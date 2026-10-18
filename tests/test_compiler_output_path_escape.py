"""FIX-4：out= 契约逃逸测试——必须是 workspace 内的相对路径（防存在性 oracle / 契约逃逸）。"""

import sys
import unittest
from pathlib import Path

TESTS_DIRECTORY = Path(__file__).resolve().parent
if str(TESTS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TESTS_DIRECTORY))  # 供 import compiler_validator_support

from compiler_validator_support import (  # noqa: E402  (负责把 src/ 加进 sys.path)
    ValidationErrorAssertions,
    collect_errors_for,
    compile_body,
)


class OutputPathEscapeTest(ValidationErrorAssertions, unittest.TestCase):
    """FIX-4：out= 必须是 workspace 内的相对路径（防存在性 oracle / 契约逃逸）。"""

    def test_parent_directory_traversal_is_rejected(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=../../outside.txt] --> B001[任务b;;out=b.txt]\n"
        )
        self.assert_single_error(errors, 8, "'..' path segments")
        self.assertEqual(len(errors), 1)

    def test_windows_drive_absolute_path_is_rejected(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=C:\\Windows\\win.ini] --> B001[任务b;;out=b.txt]\n"
        )
        self.assert_single_error(errors, 8, "must be a relative path")
        self.assertEqual(len(errors), 1)

    def test_windows_drive_relative_path_is_rejected(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=D:escape.txt] --> B001[任务b;;out=b.txt]\n"
        )
        self.assert_single_error(errors, 8, "must be a relative path")
        self.assertEqual(len(errors), 1)

    def test_posix_absolute_path_is_rejected(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=/etc/passwd] --> B001[任务b;;out=b.txt]\n"
        )
        self.assert_single_error(errors, 8, "must be a relative path")
        self.assertEqual(len(errors), 1)

    def test_windows_root_relative_path_is_rejected(self):
        # C1 回归：\Windows\win.ini 没有盘符、is_absolute() 也是 False，但
        # workspace / out 会解析到盘根 C:\Windows\win.ini —— 契约整个跑到
        # workspace 外边，缺件存在性门形同虚设。
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=\\Windows\\win.ini] --> B001[任务b;;out=b.txt]\n"
        )
        self.assert_single_error(errors, 8, "must be a relative path")
        self.assertEqual(len(errors), 1)

    def test_single_segment_root_relative_paths_are_rejected(self):
        for root_relative_path in ("\\escape.txt", "/escape.txt"):
            with self.subTest(root_relative_path=root_relative_path):
                errors = collect_errors_for(
                    "flowchart TD\n"
                    f"  A001[任务a;;out={root_relative_path}] --> "
                    "B001[任务b;;out=b.txt]\n"
                )
                self.assert_single_error(errors, 8, "must be a relative path")
                self.assertEqual(len(errors), 1)

    def test_unc_share_path_is_still_rejected(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=\\\\server\\share\\x.txt] --> B001[任务b;;out=b.txt]\n"
        )
        self.assert_single_error(errors, 8, "must be a relative path")
        self.assertEqual(len(errors), 1)

    def test_workspace_relative_forms_are_all_accepted(self):
        flow_ir = compile_body(
            "flowchart TD\n"
            "  A001[任务a;;out=out/a.txt,a\\b.txt,./c.txt] --> "
            "B001[任务b;;out=docs/d.md]\n"
        )
        self.assertEqual(
            flow_ir.nodes["A001"].outputs, ["out/a.txt", "a\\b.txt", "./c.txt"]
        )
        self.assertEqual(flow_ir.nodes["B001"].outputs, ["docs/d.md"])

    def test_nested_relative_paths_are_kept(self):
        flow_ir = compile_body(
            "flowchart TD\n"
            "  A001[任务a;;out=src/auth.py,test/auth.spec.ts] --> B001[任务b;;out=docs/a.md]\n"
        )
        self.assertEqual(
            flow_ir.nodes["A001"].outputs, ["src/auth.py", "test/auth.spec.ts"]
        )
        self.assertEqual(flow_ir.nodes["B001"].outputs, ["docs/a.md"])

    def test_second_bad_output_in_a_list_is_reported(self):
        errors = collect_errors_for(
            "flowchart TD\n"
            "  A001[任务a;;out=ok.txt,../escape.txt] --> B001[任务b;;out=b.txt]\n"
        )
        self.assert_single_error(errors, 8, "'../escape.txt'")
        self.assertEqual(len(errors), 1)


if __name__ == "__main__":
    unittest.main()
