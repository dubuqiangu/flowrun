"""Hand-written parser for the YAML frontmatter subset (interfaces.md §2).

Supported subset (all hand-rolled, no third-party YAML library):
- ``key: value`` lines
- exactly one level of indented mapping (``section:`` header + indented children)
- inline mappings ``{key: value, other: value}``
- inline arrays ``[a, "b", 3]``
- quoted strings (``"..."`` or ``'...'``)
- integers and bare scalar strings
- trailing ``# ...`` comments on their own line or after a value

Explicitly rejected with a line number: tabs, deeper nesting, inconsistent
indentation, block scalars (``|`` / ``>``), anchors/aliases (``&`` / ``*``),
unterminated quotes/brackets, missing values, nested inline collections,
duplicate keys/sections, and any residue after a quoted value that is not a
``#`` comment (a silent truncation would corrupt run_name / idempotency keys).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_INTEGER_PATTERN = re.compile(r"-?\d+")


class FrontmatterError(ValueError):
    """Frontmatter parse error carrying the source line number."""

    def __init__(self, message: str, line_number: int):
        super().__init__(f"frontmatter line {line_number}: {message}")
        self.message = message
        self.line_number = line_number


@dataclass
class FrontmatterResult:
    document: dict
    # "key" for top-level keys, "section.child" for indented keys -> source line
    key_line_numbers: dict[str, int]


def separate_frontmatter(
    source_text: str,
) -> tuple[list[tuple[int, str]], list[tuple[int, str]], bool]:
    """Split the source into (frontmatter_lines, body_lines, had_frontmatter).

    Lines are returned as ``(original_line_number, text)`` pairs so later parse
    stages can report errors against the original Mermaid source numbering.
    """
    numbered_lines = [
        (line_index + 1, line_text)
        for line_index, line_text in enumerate(source_text.splitlines())
    ]
    if not numbered_lines or numbered_lines[0][1].strip() != "---":
        return [], numbered_lines, False
    for closing_position in range(1, len(numbered_lines)):
        if numbered_lines[closing_position][1].strip() == "---":
            return (
                numbered_lines[1:closing_position],
                numbered_lines[closing_position + 1 :],
                True,
            )
    raise FrontmatterError("frontmatter opened with '---' but never closed with '---'", 1)


def parse_frontmatter_document(
    frontmatter_lines: list[tuple[int, str]],
) -> FrontmatterResult:
    """Parse the frontmatter block into a plain dict plus key->line mapping."""
    document: dict = {}
    key_line_numbers: dict[str, int] = {}
    section_key: str | None = None
    section_indent: int | None = None
    for line_number, line_text in frontmatter_lines:
        stripped_text = line_text.strip()
        if not stripped_text or stripped_text.startswith("#"):
            continue
        if "\t" in line_text:
            raise FrontmatterError(
                "tabs are not allowed in frontmatter; indent with spaces", line_number
            )
        indent = len(line_text) - len(line_text.lstrip(" "))
        key_text, had_separator, value_text = _split_key_and_value(stripped_text, line_number)
        if indent == 0:
            section_key = None
            section_indent = None
            _require_key_is_new(document, key_text, key_line_numbers, line_number)
            if had_separator:
                document[key_text] = _parse_scalar(value_text, line_number, allow_collections=True)
            else:
                document[key_text] = {}
                section_key = key_text
            key_line_numbers[key_text] = line_number
            continue
        if section_key is None:
            raise FrontmatterError(
                "unexpected indentation: top-level keys must start at column 0", line_number
            )
        if section_indent is None:
            section_indent = indent
        if indent > section_indent:
            raise FrontmatterError(
                "nested mappings are not supported by the frontmatter subset", line_number
            )
        if indent != section_indent:
            raise FrontmatterError(
                f"inconsistent indentation inside section '{section_key}'", line_number
            )
        if not had_separator:
            raise FrontmatterError(
                "nested mappings are not supported by the frontmatter subset", line_number
            )
        _require_key_is_new(
            document[section_key], key_text, key_line_numbers, line_number,
            section_key=section_key,
        )
        document[section_key][key_text] = _parse_scalar(value_text, line_number, allow_collections=True)
        key_line_numbers[f"{section_key}.{key_text}"] = line_number
    return FrontmatterResult(document=document, key_line_numbers=key_line_numbers)


def _require_key_is_new(
    mapping: dict,
    key_text: str,
    key_line_numbers: dict[str, int],
    line_number: int,
    section_key: str | None = None,
) -> None:
    """Reject a duplicate key: a silent last-wins overwrite can drop agents entirely."""
    if key_text not in mapping:
        return
    full_key = f"{section_key}.{key_text}" if section_key else key_text
    first_line = key_line_numbers.get(full_key, line_number)
    where = f" in section '{section_key}'" if section_key else ""
    raise FrontmatterError(
        f"duplicate key '{key_text}'{where} (first defined at line {first_line}); "
        "the frontmatter subset does not allow a key to be redefined",
        line_number,
    )


def _split_key_and_value(
    stripped_text: str, line_number: int
) -> tuple[str, bool, str]:
    """Split ``key: value`` into (key, had_separator, value). ``key:`` alone = section header."""
    if stripped_text[0] in "\"'":
        key_text, remainder = _take_quoted_string(stripped_text, line_number)
        remainder = remainder.strip()
        if remainder.startswith(":"):
            return key_text, True, remainder[1:]
        if remainder:
            raise FrontmatterError(
                f"unexpected text after the closing quote of quoted key '{key_text}'",
                line_number,
            )
        return key_text, False, ""
    key_text, separator, remainder = stripped_text.partition(":")
    key_text = key_text.strip()
    if not key_text:
        raise FrontmatterError("missing key before ':'", line_number)
    remainder = remainder.strip()
    if not remainder:
        return key_text, False, ""
    return key_text, True, remainder


def _take_quoted_string(text: str, line_number: int) -> tuple[str, str]:
    """Consume a leading quoted string; return (content, text after closing quote)."""
    quote_char = text[0]
    closing_position = text.find(quote_char, 1)
    if closing_position == -1:
        raise FrontmatterError(
            f"unterminated quoted string starting with {quote_char}", line_number
        )
    return text[1:closing_position], text[closing_position + 1 :]


def _parse_scalar(value_text: str, line_number: int, *, allow_collections: bool) -> object:
    value_text = value_text.strip()
    if not value_text:
        raise FrontmatterError("missing value after ':'", line_number)
    first_char = value_text[0]
    if first_char in "&*":
        raise FrontmatterError(
            "YAML anchors/aliases are not supported by the frontmatter subset", line_number
        )
    if first_char in "|>":
        raise FrontmatterError(
            "YAML block scalars are not supported by the frontmatter subset", line_number
        )
    if first_char == "{":
        if not allow_collections:
            raise FrontmatterError(
                "nested inline collections are not supported by the frontmatter subset",
                line_number,
            )
        return _parse_inline_mapping(value_text, line_number)
    if first_char == "[":
        if not allow_collections:
            raise FrontmatterError(
                "nested inline collections are not supported by the frontmatter subset",
                line_number,
            )
        return _parse_inline_array(value_text, line_number)
    if first_char in "\"'":
        content, remainder = _take_quoted_string(value_text, line_number)
        _require_only_comment_remainder(remainder, line_number)
        return content
    bare_value = _strip_inline_comment(value_text)
    if not bare_value:
        raise FrontmatterError("missing value after ':'", line_number)
    if _INTEGER_PATTERN.fullmatch(bare_value):
        return int(bare_value)
    return bare_value


def _require_only_comment_remainder(remainder: str, line_number: int) -> None:
    """A quoted value may only be followed by whitespace or a ``#`` comment.

    Anything else means the quote pair we consumed was not the intended value
    boundary (e.g. ``run: "re\\"lease x"``), so silently keeping the truncated
    content would corrupt run_name / idempotency keys / the ledger.
    """
    trailing_text = remainder.strip()
    if not trailing_text or trailing_text.startswith("#"):
        return
    raise FrontmatterError(
        f"unexpected text after the closing quote: '{trailing_text[:30]}'",
        line_number,
    )


def _strip_inline_comment(value_text: str) -> str:
    """Drop a trailing `` # ...`` comment from a bare (unquoted) scalar."""
    comment_position = value_text.find(" #")
    if comment_position == -1:
        return value_text
    return value_text[:comment_position].rstrip()


def _parse_inline_mapping(value_text: str, line_number: int) -> dict:
    if not value_text.endswith("}"):
        raise FrontmatterError(
            f"unterminated inline mapping: expected a closing '}}' in '{value_text[:30]}'",
            line_number,
        )
    inner_text = value_text[1:-1].strip()
    mapping: dict = {}
    if not inner_text:
        return mapping
    for item_text in _split_top_level_commas(inner_text):
        item_text = item_text.strip()
        if not item_text:
            raise FrontmatterError("empty item in inline mapping", line_number)
        item_key, had_separator, item_value = _split_key_and_value(item_text, line_number)
        if not had_separator:
            raise FrontmatterError(
                f"missing ':' in inline mapping item '{item_text}'", line_number
            )
        mapping[item_key] = _parse_scalar(item_value, line_number, allow_collections=False)
    return mapping


def _parse_inline_array(value_text: str, line_number: int) -> list:
    if not value_text.endswith("]"):
        raise FrontmatterError(
            f"unterminated inline array: expected a closing ']' in '{value_text[:30]}'",
            line_number,
        )
    inner_text = value_text[1:-1].strip()
    if not inner_text:
        return []
    items: list = []
    for item_text in _split_top_level_commas(inner_text):
        items.append(_parse_scalar(item_text, line_number, allow_collections=False))
    return items


def _split_top_level_commas(text: str) -> list[str]:
    """Split on commas that are outside quotes and outside nested brackets."""
    items: list[str] = []
    bracket_depth = 0
    quote_char: str | None = None
    item_start = 0
    for position, char in enumerate(text):
        if quote_char is not None:
            if char == quote_char:
                quote_char = None
            continue
        if char in "\"'":
            quote_char = char
            continue
        if char in "[{(":
            bracket_depth += 1
            continue
        if char in "]})":
            bracket_depth -= 1
            continue
        if char == "," and bracket_depth == 0:
            items.append(text[item_start:position])
            item_start = position + 1
    items.append(text[item_start:])
    return items
