"""mermaid_parser 测试共享支撑：路径引导、样例 frontmatter、"样例 frontmatter + 正文" 解析入口。

被 test_mermaid_*.py 复用（文件名不带 test_ 前缀，避免被 unittest 收集）。
"""

import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from flowrun.compiler.mermaid_parser import parse_flowchart  # noqa: E402

SAMPLE_FRONTMATTER = """---
run: release-login
defaults: {agent: claude-code, budget_tokens: 1000000}
agents:
  claude-code: {cmd: "claude -p --output-format stream-json", resume: session}
  opencode: {cmd: "opencode run"}
---
"""


def parse_body(body_text: str):
    """Parse a flowchart body on top of the sample frontmatter."""
    return parse_flowchart(SAMPLE_FRONTMATTER + body_text)
