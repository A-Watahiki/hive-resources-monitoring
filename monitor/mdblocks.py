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
separated by blank lines.
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

    Each block: {"type": ..., "level"?: int, span_key: [...spans...]}.
    Falls back to the block's "spans" if span_key is missing (e.g. an
    untranslated block).
    """
    lines: list[str] = []
    for b in blocks:
        btype = b["type"]
        if btype == "divider":
            lines.append("---")
            lines.append("")
            continue
        spans = b.get(span_key) or b.get("spans") or []
        text = inline_to_markdown(spans)
        if not text.strip():
            continue
        if btype == "heading":
            level = min(int(b.get("level", 1)), 3)
            lines.append("#" * level + " " + text)
        else:
            lines.append(_PREFIX.get(btype, "") + text)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


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


def markdown_to_notion_blocks(md: str) -> list[dict]:
    """Parse the markdown subset into Notion block objects."""
    blocks: list[dict] = []
    for raw in md.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.strip() == "---":
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            continue
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            blocks.append(_block(f"heading_{level}", parse_inline_markdown(m.group(2))))
            continue
        m = _BULLET_RE.match(line)
        if m:
            blocks.append(_block("bulleted_list_item", parse_inline_markdown(m.group(1))))
            continue
        m = _NUMBERED_RE.match(line)
        if m:
            blocks.append(_block("numbered_list_item", parse_inline_markdown(m.group(1))))
            continue
        m = _QUOTE_RE.match(line)
        if m:
            blocks.append(_block("quote", parse_inline_markdown(m.group(1))))
            continue
        blocks.append(_block("paragraph", parse_inline_markdown(line)))
    return blocks
