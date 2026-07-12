#!/usr/bin/env python3
"""
Shared helpers for a small, controlled Markdown subset that is the canonical
representation of a translated page.

The subset is deliberately narrow so the round trip
    blocks -> markdown (translate.py) -> Notion blocks (notion_sync.py)
is stable and fully testable offline:

    # / ## / ###    headings (levels 1-3)
    #> / ##> / ###> toggle headings (collapsible; children indented below)
    >>> summary     plain toggle (collapsible; children indented below)
    !!!(icon|color) callout (colored background box; icon may be empty)
    - item          bulleted list item
    1. item         numbered list item
    > quote         quote
    ---             divider
    ![alt](url)     image (must be the whole line; external URL)
    | a | b |       table row; consecutive rows form one table, and a
                    | --- | --- | line after the first row marks it a header
    |||             column container; each || child (indented) is one column
    ||              a single column inside a ||| container
    (anything else) paragraph

Inline: **bold** and [text](url) links. One block per line; blocks are
separated by blank lines (table rows are the exception: consecutive lines).
A block may have "children" (e.g. a numbered/bulleted item's body paragraph,
or a toggle's collapsed content) — those are serialized indented two spaces
per nesting level, and re-associated with their parent on parse by indent
depth. This keeps consecutive list items adjacent Notion blocks (so Notion's
auto-numbering stays continuous) while still carrying a body along with its
item.
"""

from __future__ import annotations

import re

# Notion caps a single rich_text content string at 2000 chars.
RICH_TEXT_LIMIT = 2000
# Notion caps children per append/create request at 100.
TABLE_ROW_LIMIT = 100

_ESCAPE_RE = re.compile(r"([\\\[\]*])")
_UNESCAPE_RE = re.compile(r"\\(.)")

# A link [text](url) or a bold **run** (bold run stops at the next "**").
_INLINE_RE = re.compile(
    r"\[(?P<ltext>(?:\\.|[^\]\\])*)\]\((?P<url>[^)]*)\)"
    r"|\*\*(?P<btext>(?:\\.|[^*\\]|\*(?!\*))+)\*\*"
)

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
_TOGGLE_HEADING_RE = re.compile(r"^(#{1,3})>\s+(.*)$")
_TOGGLE_RE = re.compile(r"^>>>\s+(.*)$")
_CALLOUT_RE = re.compile(r"^!!!\(([^|)]*)\|([^)]*)\)\s?(.*)$")
_IMAGE_RE = re.compile(r"^!\[((?:\\.|[^\]\\])*)\]\((\S+)\)$")
_BULLET_RE = re.compile(r"^[-*]\s+(.*)$")
_NUMBERED_RE = re.compile(r"^\d+\.\s+(.*)$")
_QUOTE_RE = re.compile(r"^>\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\|(\s*-{3,}\s*\|)+$")
_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")

NOTION_CALLOUT_COLORS = {
    "gray_background", "brown_background", "orange_background",
    "yellow_background", "green_background", "blue_background",
    "purple_background", "pink_background", "red_background", "default",
}


# --------------------------------------------------------------------------- #
# Serialization: spans -> markdown
# --------------------------------------------------------------------------- #
def _escape(text: str) -> str:
    return _ESCAPE_RE.sub(r"\\\1", text)


def _unescape(text: str) -> str:
    return _UNESCAPE_RE.sub(r"\1", text)


def inline_to_markdown(spans: list[dict]) -> str:
    """spans: [{"text": str, "href"?: str, "bold"?: bool}] -> markdown string."""
    out = []
    for s in spans:
        t = _escape(s.get("text", ""))
        if not t and not s.get("href"):
            continue
        if s.get("bold"):
            t = f"**{t}**"
        if s.get("href"):
            t = f"[{t}]({s['href']})"
        out.append(t)
    return "".join(out)


_PREFIX = {
    "paragraph": "",
    "bulleted": "- ",
    "numbered": "1. ",
    "quote": "> ",
    "toggle": ">>> ",
}


def blocks_to_markdown(blocks: list[dict], span_key: str) -> str:
    """Serialize block dicts to the markdown subset.

    Each block: {"type": ..., "level"?: int, span_key: [...spans...],
    "children"?: [...nested blocks...]}. Falls back to the block's "spans"
    if span_key is missing (e.g. an untranslated block).
    """
    lines = _serialize_blocks(blocks, span_key, 0)
    return "\n".join(lines).strip() + "\n"


def _cell_md(cell: dict, span_key: str) -> str:
    text = inline_to_markdown(cell.get(span_key) or cell.get("spans") or [])
    return text.replace("|", "\\|")


def _serialize_blocks(blocks: list[dict], span_key: str, level: int) -> list[str]:
    indent = "  " * level
    lines: list[str] = []
    for b in blocks:
        btype = b["type"]
        children = b.get("children") or []
        if btype == "divider":
            lines.append(indent + "---")
            lines.append("")
            continue
        if btype == "image":
            if b.get("url"):
                lines.append(indent + f"![{_escape(b.get('alt') or 'image')}]"
                                      f"({b['url']})")
                lines.append("")
            continue
        if btype == "columns":
            lines.append(indent + "|||")
            lines.append("")
            lines.extend(_serialize_blocks(children, span_key, level + 1))
            continue
        if btype == "column":
            lines.append(indent + "||")
            lines.append("")
            lines.extend(_serialize_blocks(children, span_key, level + 1))
            continue
        if btype == "table":
            rows = b.get("rows") or []
            for i, row in enumerate(rows):
                lines.append(indent + "| "
                             + " | ".join(_cell_md(c, span_key) for c in row)
                             + " |")
                if i == 0 and b.get("header"):
                    lines.append(indent + "| " + " | ".join(["---"] * len(row))
                                 + " |")
            lines.append("")
            continue
        spans = b.get(span_key) or b.get("spans") or []
        text = inline_to_markdown(spans)
        if not text.strip() and not children:
            continue
        if text.strip():
            if btype == "heading":
                lvl = min(int(b.get("level", 1)), 3)
                marker = "#" * lvl + (">" if b.get("toggle") else "")
                lines.append(indent + marker + " " + text)
            elif btype == "callout":
                icon = (b.get("icon") or "").replace("|", "").replace(")", "")
                color = b.get("color") or "gray_background"
                lines.append(indent + f"!!!({icon}|{color}) " + text)
            else:
                lines.append(indent + _PREFIX.get(btype, "") + text)
            lines.append("")
        if children:
            lines.extend(_serialize_blocks(children, span_key, level + 1))
    return lines


# --------------------------------------------------------------------------- #
# Parsing: markdown -> Notion blocks
# --------------------------------------------------------------------------- #
def parse_inline_markdown(text: str) -> list[dict]:
    """markdown string -> [{"content": str, "href"?: str, "bold"?: bool}]."""
    spans: list[dict] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            spans.append({"content": _unescape(text[pos:m.start()])})
        if m.group("url") is not None:
            inner = m.group("ltext")
            bold = len(inner) >= 4 and inner.startswith("**") and inner.endswith("**")
            content = inner[2:-2] if bold else inner
            span = {"content": _unescape(content), "href": m.group("url")}
            if bold:
                span["bold"] = True
            spans.append(span)
        else:
            spans.append({"content": _unescape(m.group("btext")), "bold": True})
        pos = m.end()
    if pos < len(text):
        spans.append({"content": _unescape(text[pos:])})
    return [s for s in spans if s.get("content") or s.get("href")]


def _rich_text(spans: list[dict]) -> list[dict]:
    rich: list[dict] = []
    for s in spans:
        content = s.get("content", "")
        href = s.get("href")
        valid_link = bool(href) and href.startswith(("http://", "https://"))
        # Chunk overly long content to respect Notion's per-item limit.
        for i in range(0, max(len(content), 1), RICH_TEXT_LIMIT):
            chunk = content[i:i + RICH_TEXT_LIMIT]
            if not chunk and (i > 0 or content):
                continue
            text_obj: dict = {"content": chunk}
            if valid_link:
                text_obj["link"] = {"url": href}
            item: dict = {"type": "text", "text": text_obj}
            if s.get("bold"):
                item["annotations"] = {"bold": True}
            rich.append(item)
    return rich or [{"type": "text", "text": {"content": ""}}]


def _block(kind: str, spans: list[dict]) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": _rich_text(spans)}}


class _TableRow:
    """Sentinel returned by _parse_one_line for a table row / separator line;
    merged into a table block by the main parse loop."""

    def __init__(self, cells: list[list[dict]] | None, separator: bool = False):
        self.cells = cells or []
        self.separator = separator


def _parse_table_line(line: str) -> _TableRow:
    if _TABLE_SEP_RE.match(line.replace(" ", "")):
        return _TableRow(None, separator=True)
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|") and not inner.endswith("\\|"):
        inner = inner[:-1]
    cells = [_rich_text(parse_inline_markdown(c.strip()))
             for c in _CELL_SPLIT_RE.split(inner)]
    return _TableRow(cells)


def _parse_one_line(line: str):
    if line.strip() == "---":
        return {"object": "block", "type": "divider", "divider": {}}
    if line == "|||":
        return {"object": "block", "type": "column_list", "column_list": {}}
    if line == "||":
        return {"object": "block", "type": "column", "column": {}}
    if line.startswith("|"):
        return _parse_table_line(line)
    m = _IMAGE_RE.match(line)
    if m:
        return {"object": "block", "type": "image",
                "image": {"type": "external", "external": {"url": m.group(2)}}}
    m = _TOGGLE_HEADING_RE.match(line)
    if m:
        block = _block(f"heading_{len(m.group(1))}",
                       parse_inline_markdown(m.group(2)))
        block[f"heading_{len(m.group(1))}"]["is_toggleable"] = True
        return block
    m = _HEADING_RE.match(line)
    if m:
        return _block(f"heading_{len(m.group(1))}", parse_inline_markdown(m.group(2)))
    m = _TOGGLE_RE.match(line)
    if m:
        return _block("toggle", parse_inline_markdown(m.group(1)))
    m = _CALLOUT_RE.match(line)
    if m:
        icon, color, text = m.group(1).strip(), m.group(2).strip(), m.group(3)
        block = _block("callout", parse_inline_markdown(text))
        if icon:
            block["callout"]["icon"] = {"type": "emoji", "emoji": icon}
        if color in NOTION_CALLOUT_COLORS and color != "default":
            block["callout"]["color"] = color
        return block
    m = _BULLET_RE.match(line)
    if m:
        return _block("bulleted_list_item", parse_inline_markdown(m.group(1)))
    m = _NUMBERED_RE.match(line)
    if m:
        return _block("numbered_list_item", parse_inline_markdown(m.group(1)))
    m = _QUOTE_RE.match(line)
    if m:
        return _block("quote", parse_inline_markdown(m.group(1)))
    return _block("paragraph", parse_inline_markdown(line))


def _drop_empty_children(blocks: list[dict]) -> None:
    for b in blocks:
        children = b.get("children")
        if children:
            _drop_empty_children(children)
        else:
            b.pop("children", None)


def _finalize_structures(blocks: list[dict]) -> list[dict]:
    """Post-parse fixups that need the whole tree (returns the fixed list):

    - Pad table rows to a uniform width (Notion requires it).
    - Move a column container's parsed children into the nested shape the
      Notion API expects (column_list -> {"column_list": {"children":
      [column...]}}; column -> {"column": {"children": [...]}}). Content
      deeper than one level inside a column is hoisted flat — the API only
      allows two nesting levels in the creation payload.
    - A stray || column outside a ||| container is replaced by its content.
    """
    out: list[dict] = []
    for b in blocks:
        if b["type"] == "table":
            width = max((len(r["table_row"]["cells"])
                         for r in b["table"]["children"]), default=0)
            b["table"]["table_width"] = width
            for r in b["table"]["children"]:
                cells = r["table_row"]["cells"]
                while len(cells) < width:
                    cells.append([{"type": "text", "text": {"content": ""}}])
            out.append(b)
            continue
        if b["type"] == "column_list":
            columns = [c for c in b.pop("children", [])
                       if c["type"] == "column"]
            for col in columns:
                kids = _finalize_structures(_hoist_flat(col.pop("children", [])))
                col["column"] = {"children": kids}
            if columns:
                b["column_list"] = {"children": columns}
                out.append(b)
            continue
        if b["type"] == "column":
            out.extend(_finalize_structures(b.pop("children", [])))
            continue
        if b.get("children"):
            b["children"] = _finalize_structures(b["children"])
        out.append(b)
    return out


def _hoist_flat(blocks: list[dict]) -> list[dict]:
    out: list[dict] = []
    for b in blocks:
        kids = b.pop("children", [])
        out.append(b)
        out.extend(_hoist_flat(kids))
    return out


def markdown_to_notion_blocks(md: str) -> list[dict]:
    """Parse the markdown subset into Notion block objects.

    A line indented two spaces deeper than the preceding block becomes a
    child of it (nested under that block's "children"), matching how
    blocks_to_markdown serializes nested content. Consecutive table-row
    lines at the same indent merge into a single table block.
    """
    root: list[dict] = []
    stack: list[tuple[int, list[dict]]] = [(-1, root)]
    for raw in md.split("\n"):
        if not raw.strip():
            continue
        stripped = raw.lstrip(" ")
        indent = (len(raw) - len(stripped)) // 2
        line = stripped.rstrip()
        while len(stack) > 1 and stack[-1][0] >= indent:
            stack.pop()
        parsed = _parse_one_line(line)
        container = stack[-1][1]
        if isinstance(parsed, _TableRow):
            last = container[-1] if container else None
            if last is None or last.get("type") != "table":
                if parsed.separator:
                    continue
                last = {"object": "block", "type": "table",
                        "table": {"table_width": 0, "has_column_header": False,
                                  "has_row_header": False, "children": []}}
                container.append(last)
            if parsed.separator:
                last["table"]["has_column_header"] = True
            elif len(last["table"]["children"]) < TABLE_ROW_LIMIT:
                last["table"]["children"].append(
                    {"object": "block", "type": "table_row",
                     "table_row": {"cells": parsed.cells}})
            continue
        parsed["children"] = []
        container.append(parsed)
        stack.append((indent, parsed["children"]))
    root = _finalize_structures(root)
    _drop_empty_children(root)
    return root
