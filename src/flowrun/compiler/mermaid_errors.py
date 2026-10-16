"""Compile-time error type shared by the Mermaid compiler modules.

Lives in its own module so line-level parsers (mermaid_syntax,
mermaid_edge_label) and the document parser (mermaid_parser) can all raise
the same class without importing each other in a cycle. MermaidParseError is
re-exported from flowrun.compiler.mermaid_syntax and
flowrun.compiler.mermaid_parser for backwards compatibility.
"""

from __future__ import annotations


class MermaidParseError(ValueError):
    """Mermaid parse error carrying the original source line number."""

    def __init__(self, message: str, line_number: int):
        super().__init__(f"line {line_number}: {message}")
        self.message = message
        self.line_number = line_number
