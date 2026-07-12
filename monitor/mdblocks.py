#!/usr/bin/env python3
"""
Shared helpers for a small, controlled Markdown subset that is the canonical
representation of a translated page.

The subset is deliberately narrow so the round trip
    blocks -> markdown (translate.py) -> Notion blocks (notion_sync.py)
is stable and fully testable offline:

    # / ## / ###   headings (levels 1-3)
    - item          bulleted list item
    1. item         numbered list item
    > quote         quote
    ---             divider
    (anything else) paragraph

Inline: **bold** and [text](url) links. One block per line; blocks are
separated by blank lines. A block may have "children" (e.g. a numbered/
bulleted item's body paragraph) — those are serialized indented two spaces
per nesting level, and re-associated with their parent on parse by indent
depth. This keeps consecutive list items adjacent Notion blocks (so Notion's
auto-numbering stays continuous) while still carrying a body along with its
item.
"""

from __future__ import annotations

import re

# Notion caps a single rich_text content string at 2000 chars.
RICH_TEXT_LIMIT = 2000

_ESCAPE_RE = re.compile(r"([\\\[\]*])")
_UNESCAPE_RE = re.compile(r"\\(.)")

# A link [text](url) or a bold **run** (bold run stops at the next "**").
_INLINE_RE = re.compile(
    r"\[(?P<ltext>(?:\\.|[^\]\\])*)\]\((?P<url>[^)]*)\)"
    r"|\*\*(?P<btext>(?:\\.|[^*\\]|\*(?!\*))+)\*\*"
)

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
_BULLET_RE = re.compile(r"^[-*]\s+(.*)$")
_NUMBERED_RE = re.compile(r"^\d+\.\s+(.*)$")
_QUOTE_RE = re.compile(r"^>\s+(.*)$")


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
}


def blocks_to_markdown(blocks: list[dict], span_key: str) -> str:
    """Serialize block dicts to the markdown subset.

    Each block: {"type": ..., "level"?: int, span_key: [...spans...],
    "children"?: [...nested blocks...]}. Falls back to the block's "spans"
    if span_key is missing (e.g. an untranslated block).
    """
    lines = _serialize_blocks(blocks, span_key, 0)
    return "\n".join(lines).strip() + "\n"


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
        spans = b.get(span_key) or b.get("spans") or []
        text = inline_to_markdown(spans)
        if not text.strip() and not children:
            continue
        if text.strip():
            if btype == "heading":
                lvl = min(int(b.get("level", 1)), 3)
                lines.append(indent + "#" * lvl + " " + text)
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


def _parse_one_line(line: str) -> dict:
    if line.strip() == "---":
        return {"object": "block", "type": "divider", "divider": {}}
    m = _HEADING_RE.match(line)
    if m:
        return _block(f"heading_{len(m.group(1))}", parse_inline_markdown(m.group(2)))
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


def markdown_to_notion_blocks(md: str) -> list[dict]:
    """Parse the markdown subset into Notion block objects.

    A line indented two spaces deeper than the preceding block becomes a
    child of it (nested under that block's "children"), matching how
    blocks_to_markdown serializes nested content.
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
        block = _parse_one_line(line)
        block["children"] = []
        stack[-1][1].append(block)
        stack.append((indent, block["children"]))
    _drop_empty_children(root)
    return root
