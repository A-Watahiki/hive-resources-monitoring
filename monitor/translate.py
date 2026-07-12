#!/usr/bin/env python3
"""
DeepL translation pipeline for crawled Hive Resource Library pages.

Unlike a naive text dump, this re-fetches each page's HTML and extracts a
*structured* block list (headings, list items, quotes, dividers, paragraphs
with inline links) so the layout of the original Hive page is preserved when
the translation is rendered into Notion.

For each page that is new or whose content changed (per the monitor snapshot
hash), or that predates the current schema, it:

  1. fetches the page HTML and extracts structured blocks,
  2. translates each block's inline content via the DeepL API (HTML tag
     handling, so inline links/formatting survive translation),
  3. stores source + translation as a small Markdown subset (canonical,
     human-reviewable) in translations/pages/<page>.json.

A per-run character budget keeps daily runs inside the DeepL free tier; the
backlog is worked through gradually.

Environment:
    DEEPL_API_KEY          required. Keys ending in ":fx" use the free API host.
    TRANSLATE_CHAR_BUDGET  optional override of the per-run character budget.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup, NavigableString

import mdblocks

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "monitor" / "config.yaml"
STATE_PATH = ROOT / "snapshots" / "state.json"
TRANSLATIONS_DIR = ROOT / "translations" / "pages"

# Bump when the stored JSON shape changes so old entries get re-translated.
SCHEMA_VERSION = 6

# DeepL: max number of `text` params per request.
DEEPL_BATCH = 40

SKIP_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "aside",
             "form", "svg", "template", "iframe", "button", "head"}
BLOCK_DESCENDANTS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "ol",
                     "blockquote", "hr", "li", "table")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def load_config() -> tuple[dict, dict]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    tcfg = cfg.get("translation") or {}
    tcfg.setdefault("include_prefixes", ["https://resources.joinhive.org/library"])
    tcfg.setdefault("exclude_url_patterns", [])
    tcfg.setdefault("target_lang", "JA")
    tcfg.setdefault("char_budget_per_run", 40000)
    tcfg.setdefault("min_text_length", 40)
    tcfg.setdefault("request_interval_seconds", 0.5)
    tcfg.setdefault("user_agent", cfg.get("user_agent", "HiveResourceMonitor/1.0"))
    tcfg.setdefault("request_timeout", cfg.get("request_timeout", 30))
    tcfg.setdefault("language", cfg.get("language", "en"))
    return tcfg


def slug_for_url(url: str) -> str:
    path = urlparse(url).path.strip("/") or "root"
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", path)[:80]
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{digest}"


def url_in_scope(url: str, tcfg: dict) -> bool:
    """Whether url falls within the set of pages this pipeline translates
    (regardless of whether it has actually been translated yet)."""
    if not any(url.startswith(p) for p in tcfg["include_prefixes"]):
        return False
    for pat in tcfg["exclude_url_patterns"]:
        if re.search(pat, url):
            return False
    return True


def is_eligible(url: str, page: dict, tcfg: dict) -> bool:
    if not url_in_scope(url, tcfg):
        return False
    text = page.get("text", "")
    if text.startswith("[binary resource"):
        return False
    if len(text) < tcfg["min_text_length"]:
        return False
    return True


# --------------------------------------------------------------------------- #
# HTML -> structured blocks
# --------------------------------------------------------------------------- #
def _normalize_spans(spans: list[dict]) -> list[dict]:
    """Collapse whitespace, merge adjacent same-style spans, trim ends."""
    merged: list[dict] = []
    for s in spans:
        text = re.sub(r"\s+", " ", s["text"])
        if not text:
            continue
        key = (s.get("href"), bool(s.get("bold")))
        if merged and (merged[-1].get("href"), bool(merged[-1].get("bold"))) == key:
            merged[-1]["text"] += text
        else:
            span = {"text": text}
            if s.get("href"):
                span["href"] = s["href"]
            if s.get("bold"):
                span["bold"] = True
            merged.append(dict(s, text=text))
    # Trim leading/trailing whitespace across the whole block.
    if merged:
        merged[0]["text"] = merged[0]["text"].lstrip()
        merged[-1]["text"] = merged[-1]["text"].rstrip()
    return [s for s in merged if s["text"]]


def _anchor_span(a, base_url: str, bold: bool) -> dict | None:
    txt = a.get_text()
    if not txt.strip():
        return None
    span = {"text": txt, "bold": bold or bool(a.find(["b", "strong"]))}
    href = a.get("href")
    if href and not href.startswith(("javascript:", "#")):
        span["href"] = urljoin(base_url, href)
    return span


def _inline_spans(el, base_url: str) -> list[dict]:
    # A block-level walk may hand us the <a> tag itself (e.g. a whole card/
    # list item is one anchor) rather than a container that merely contains
    # one — recursing into its children would silently drop that href.
    if getattr(el, "name", None) == "a":
        span = _anchor_span(el, base_url, False)
        return _normalize_spans([span]) if span else []

    spans: list[dict] = []

    def rec(node, bold: bool) -> None:
        for c in node.children:
            if isinstance(c, NavigableString):
                spans.append({"text": str(c), "bold": bold})
            elif c.name in SKIP_TAGS:
                continue
            elif c.name == "br":
                spans.append({"text": " ", "bold": bold})
            elif c.name == "a":
                span = _anchor_span(c, base_url, bold)
                if span:
                    spans.append(span)
            elif c.name in ("strong", "b"):
                rec(c, True)
            else:
                rec(c, bold)

    rec(el, False)
    return _normalize_spans(spans)


def _has_block_children(el) -> bool:
    return el.find(BLOCK_DESCENDANTS) is not None


def html_to_blocks(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(list(SKIP_TAGS)):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    blocks: list[dict] = []
    _walk(root, base_url, blocks)
    return _dedupe(blocks)


def extract_cover_url(html: str) -> str | None:
    """The page's cover image (img.notion-header__cover-image), if any —
    set as the Notion page cover so the translated page looks the same."""
    soup = BeautifulSoup(html, "html.parser")
    img = soup.find("img", class_="notion-header__cover-image")
    src = (img.get("src") or "").strip() if img else ""
    return src if src.startswith(("http://", "https://")) else None


def _walk(node, base_url: str, blocks: list[dict]) -> None:
    for child in node.find_all(recursive=False):
        _emit(child, base_url, blocks)


NOTION_COLOR_NAMES = {"gray", "brown", "orange", "yellow", "green", "blue",
                      "purple", "pink", "red"}

# img elements whose class marks them as site chrome, not page content.
_CHROME_IMG_MARKERS = ("breadcrumb", "notion-icon", "notion-header", "navbar")


def _direct_child_by_class(el, cls: str):
    for c in el.find_all(recursive=False):
        if cls in (c.get("class") or []):
            return c
    return None


def _emit_toggle(el, base_url: str) -> dict | None:
    """div.notion-toggle: a __summary (whose ‣ trigger is dropped; an inner
    h1-h3 makes it a toggle *heading*) plus __content children."""
    summary = _direct_child_by_class(el, "notion-toggle__summary")
    content = _direct_child_by_class(el, "notion-toggle__content")
    spans: list[dict] = []
    level = None
    if summary is not None:
        for trig in summary.find_all(class_="notion-toggle__trigger"):
            trig.decompose()
        h = summary.find(["h1", "h2", "h3", "h4", "h5", "h6"])
        if h is not None:
            level = min(int(h.name[1]), 3)
        spans = _inline_spans(summary, base_url)
    children: list[dict] = []
    if content is not None:
        for c in content.find_all(recursive=False):
            _emit(c, base_url, children)
    if not spans and not children:
        return None
    if level:
        return {"type": "heading", "level": level, "toggle": True,
                "spans": spans, "children": children}
    return {"type": "toggle", "spans": spans, "children": children}


def _emit_callout(el, base_url: str) -> dict | None:
    """div.notion-callout: an __icon emoji, a bg-<color>* class, and
    __content that is either inline text or nested blocks."""
    icon_el = _direct_child_by_class(el, "notion-callout__icon")
    content_el = _direct_child_by_class(el, "notion-callout__content")
    icon = (icon_el.get_text().strip() if icon_el is not None else "") or None
    color = "gray_background"
    for cls in el.get("class") or []:
        m = re.match(r"bg-([a-z]+)", cls)
        if m and m.group(1) in NOTION_COLOR_NAMES:
            color = f"{m.group(1)}_background"
            break
    inner: list[dict] = []
    if content_el is not None:
        if _has_block_children(content_el):
            _walk(content_el, base_url, inner)
        else:
            spans = _inline_spans(content_el, base_url)
            if spans:
                inner.append({"type": "paragraph", "spans": spans})
    if not inner:
        return None
    first = inner[0]
    if first["type"] == "paragraph" and not first.get("children"):
        return {"type": "callout", "icon": icon, "color": color,
                "spans": first["spans"], "children": inner[1:]}
    return {"type": "callout", "icon": icon, "color": color,
            "spans": [], "children": inner}


def _flatten_tree(blocks: list[dict]) -> list[dict]:
    out: list[dict] = []
    for b in blocks:
        kids = b.pop("children", [])
        out.append(b)
        out.extend(_flatten_tree(kids))
    return out


def _emit_columns(el, base_url: str, blocks: list[dict]) -> None:
    """div.notion-column-list > div.notion-column: side-by-side columns.
    Column content is flattened one level deep — the Notion API only allows
    two levels of nesting when creating a column layout in one request."""
    cols: list[dict] = []
    for col_el in el.find_all(recursive=False):
        if "notion-column" not in (col_el.get("class") or []):
            continue
        inner: list[dict] = []
        for c in col_el.find_all(recursive=False):
            _emit(c, base_url, inner)
        inner = _flatten_tree(inner)
        if inner:
            cols.append({"type": "column", "children": inner})
    if len(cols) >= 2:
        blocks.append({"type": "columns", "children": cols})
    elif cols:
        blocks.extend(cols[0]["children"])


def _emit_table(el, base_url: str) -> dict | None:
    rows: list[list[dict]] = []
    header = False
    for tr in el.find_all("tr"):
        cells = []
        for cell_el in tr.find_all(["th", "td"]):
            cells.append({"spans": _inline_spans(cell_el, base_url)})
            if cell_el.name == "th":
                header = True
        if cells:
            rows.append(cells)
    return {"type": "table", "header": header, "rows": rows} if rows else None


def _emit_image(img, blocks: list[dict]) -> None:
    src = (img.get("src") or "").strip()
    cls = " ".join(img.get("class") or [])
    if (src.startswith(("http://", "https://"))
            and not any(k in cls for k in _CHROME_IMG_MARKERS)):
        blocks.append({"type": "image", "url": src,
                       "alt": (img.get("alt") or "").strip()})


def _emit(child, base_url: str, blocks: list[dict]) -> None:
    name = child.name
    if not name or name in SKIP_TAGS or child.get("role") == "navigation":
        return
    classes = child.get("class") or []
    if "notion-toggle" in classes:
        block = _emit_toggle(child, base_url)
        if block:
            blocks.append(block)
        return
    if "notion-callout" in classes:
        block = _emit_callout(child, base_url)
        if block:
            blocks.append(block)
        return
    if "notion-column-list" in classes:
        _emit_columns(child, base_url, blocks)
        return
    if name == "table":
        block = _emit_table(child, base_url)
        if block:
            blocks.append(block)
        return
    if name == "img":
        _emit_image(child, blocks)
        return
    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        spans = _inline_spans(child, base_url)
        if spans:
            blocks.append({"type": "heading", "level": min(int(name[1]), 3),
                           "spans": spans})
    elif name in ("ul", "ol"):
        kind = "numbered" if name == "ol" else "bulleted"
        last_item: dict | None = None
        for c in child.find_all(recursive=False):
            if c.name == "li":
                spans = _inline_spans(c, base_url)
                if spans:
                    item = {"type": kind, "spans": spans}
                    blocks.append(item)
                    last_item = item
                else:
                    last_item = None
                # Nested lists are genuinely part of this item.
                for sub in c.find_all(("ul", "ol"), recursive=False):
                    target = (last_item.setdefault("children", [])
                             if last_item is not None else blocks)
                    _emit(sub, base_url, target)
            else:
                # Some Notion renderers put a list item's body content (e.g. a
                # prompt paragraph) as a SIBLING of the <li> inside the list,
                # not inside it. Nest it as a child of the preceding item so
                # Notion keeps the numbering continuous instead of treating
                # each title+body pair as its own one-item list.
                target = (last_item.setdefault("children", [])
                         if last_item is not None else blocks)
                _emit(c, base_url, target)
    elif name == "blockquote":
        spans = _inline_spans(child, base_url)
        if spans:
            blocks.append({"type": "quote", "spans": spans})
    elif name == "hr":
        blocks.append({"type": "divider"})
    elif name == "p":
        spans = _inline_spans(child, base_url)
        if spans:
            blocks.append({"type": "paragraph", "spans": spans})
        for img in child.find_all("img"):
            _emit_image(img, blocks)
    elif _has_block_children(child):
        _walk(child, base_url, blocks)
    else:
        spans = _inline_spans(child, base_url)
        if spans:
            blocks.append({"type": "paragraph", "spans": spans})
        for img in child.find_all("img"):
            _emit_image(img, blocks)


def _dedupe(blocks: list[dict]) -> list[dict]:
    """Drop immediately repeated identical blocks (common on Notion sites)."""
    out: list[dict] = []
    for b in blocks:
        if out and out[-1] == b:
            continue
        out.append(b)
    return out


# --------------------------------------------------------------------------- #
# DeepL
# --------------------------------------------------------------------------- #
class DeepLQuotaExceeded(Exception):
    pass


def deepl_base_url(api_key: str) -> str:
    return ("https://api-free.deepl.com" if api_key.endswith(":fx")
            else "https://api.deepl.com")


def deepl_usage(session: requests.Session, base: str) -> dict | None:
    try:
        resp = session.get(f"{base}/v2/usage", timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return None


def spans_to_html(spans: list[dict]) -> str:
    parts = []
    for s in spans:
        t = html_lib.escape(s.get("text", ""))
        if s.get("bold"):
            t = f"<b>{t}</b>"
        if s.get("href"):
            t = f'<a href="{html_lib.escape(s["href"], quote=True)}">{t}</a>'
        parts.append(t)
    return "".join(parts)


def html_to_spans(fragment: str) -> list[dict]:
    soup = BeautifulSoup(fragment, "html.parser")
    spans: list[dict] = []

    def rec(node, bold: bool) -> None:
        for c in node.children:
            if isinstance(c, NavigableString):
                spans.append({"text": str(c), "bold": bold})
            elif c.name == "a":
                txt = c.get_text()
                if txt:
                    span = {"text": txt, "bold": bold or bool(c.find(["b", "strong"]))}
                    href = c.get("href")
                    if href:
                        span["href"] = href
                    spans.append(span)
            elif c.name in ("b", "strong"):
                rec(c, True)
            else:
                rec(c, bold)

    rec(soup, False)
    return _normalize_spans(spans)


def deepl_translate_html_batch(session: requests.Session, base: str,
                               fragments: list[str], target_lang: str,
                               interval: float) -> tuple[list[str], bool]:
    """Returns (translations, quota_exhausted). On a 456 quota error the
    translations list is shorter than `fragments` — everything translated by
    the earlier batches is still returned rather than thrown away."""
    results: list[str] = []
    for i in range(0, len(fragments), DEEPL_BATCH):
        chunk = fragments[i:i + DEEPL_BATCH]
        data = [("text", f) for f in chunk]
        data += [("target_lang", target_lang), ("tag_handling", "html")]
        resp = session.post(f"{base}/v2/translate", data=data, timeout=120)
        if resp.status_code == 456:
            return results, True
        resp.raise_for_status()
        results.extend(t["text"] for t in resp.json()["translations"])
        time.sleep(interval)
    return results, False


def _clone_block_tree(blocks: list[dict]) -> list[dict]:
    out = []
    for b in blocks:
        nb = dict(b)
        if b.get("children"):
            nb["children"] = _clone_block_tree(b["children"])
        if b.get("rows"):
            nb["rows"] = [[dict(cell, spans=list(cell.get("spans") or []))
                           for cell in row] for row in b["rows"]]
        out.append(nb)
    return out


def _collect_translatable(blocks: list[dict]) -> list[dict]:
    """Refs (not copies) to every span-holder in the tree — blocks (including
    nested children, e.g. a list item's body paragraph) and table cells —
    that have text to send through DeepL."""
    out = []
    for b in blocks:
        if b["type"] != "divider" and b.get("spans"):
            out.append(b)
        for row in b.get("rows") or []:
            for cell in row:
                if cell.get("spans"):
                    out.append(cell)
        if b.get("children"):
            out.extend(_collect_translatable(b["children"]))
    return out


class PageBudgetExceeded(Exception):
    """This page's untranslated text doesn't fit the remaining budget."""


def translate_blocks(session: requests.Session, base: str, blocks: list[dict],
                     target_lang: str, interval: float,
                     cache: dict[str, str] | None = None,
                     budget_left: int | None = None) -> tuple[list[dict], int, int]:
    """Translate every span-holder in `blocks` (returns a translated copy).

    Fragments already present in `cache` — built from previously stored
    translations — are reused without a DeepL call, so re-extracting a page
    whose text didn't change costs no quota at all. Only cache misses are
    sent to DeepL (and added to the cache for later pages in the same run).

    Returns (translated_blocks, chars_sent_to_deepl, fragments_left_english).
    If the misses don't fit budget_left, raises PageBudgetExceeded before
    any API call. If DeepL reports its quota exhausted mid-page, the missed
    fragments keep their source-language spans and are counted in the third
    return value — the caller marks the page incomplete and retries on a
    later run (e.g. after the monthly quota resets).
    """
    cache = cache if cache is not None else {}
    cloned = _clone_block_tree(blocks)
    targets = _collect_translatable(cloned)
    fragments = [spans_to_html(t["spans"]) for t in targets]

    results: dict[int, str] = {}
    miss_idx = []
    for i, frag in enumerate(fragments):
        if frag in cache:
            results[i] = cache[frag]
        else:
            miss_idx.append(i)
    miss_chars = sum(len(fragments[i]) for i in miss_idx)
    if miss_idx and budget_left is not None and miss_chars > budget_left:
        raise PageBudgetExceeded(str(miss_chars))

    untranslated = 0
    if miss_idx:
        translated, quota_hit = deepl_translate_html_batch(
            session, base, [fragments[i] for i in miss_idx],
            target_lang, interval)
        for i, frag in zip(miss_idx, translated):
            results[i] = frag
            cache[fragments[i]] = frag
        untranslated = len(miss_idx) - len(translated)
        miss_chars = sum(len(fragments[i])
                         for i in miss_idx[:len(translated)])

    for i, t in enumerate(targets):
        if i in results:
            t["spans"] = html_to_spans(results[i]) or t["spans"]
    return cloned, miss_chars, untranslated


# --------------------------------------------------------------------------- #
# Translation-reuse cache (source fragment html -> translated fragment html)
# --------------------------------------------------------------------------- #
# source_markdown / translated_markdown are serialized from parallel block
# trees, so their lines pair 1:1. Each line pair yields a (source fragment,
# translated fragment) mapping that translate_blocks() can reuse — which lets
# a schema change re-extract and re-render every page without spending DeepL
# quota on text that was already translated.
_MD_LINE_PREFIX_RES = [
    re.compile(r"^#{1,3}>?\s+"),      # heading / toggle heading
    re.compile(r"^>>>\s+"),           # toggle
    re.compile(r"^!!!\([^)]*\)\s?"),  # callout
    re.compile(r"^[-*]\s+"),          # bullet
    re.compile(r"^\d+\.\s+"),         # numbered
    re.compile(r"^>\s+"),             # quote
]
_MD_SKIP_LINE_RE = re.compile(r"^(---$|\|\|\|?$|!\[)")


def _md_inline_to_html(text: str) -> str:
    spans = []
    for s in mdblocks.parse_inline_markdown(text):
        span = {"text": s.get("content", "")}
        if s.get("href"):
            span["href"] = s["href"]
        if s.get("bold"):
            span["bold"] = True
        spans.append(span)
    return spans_to_html(spans)


def _strip_leading_symbols(fragment: str) -> str:
    """Drop a leading emoji run (plus one space) from an html fragment —
    older extractions merged a callout's icon into its text, so the cached
    fragment carries the emoji while a fresh extraction doesn't."""
    i = 0
    while i < len(fragment):
        c = fragment[i]
        if c.isalnum() or c.isspace() or ord(c) < 0x2000:
            break
        i += 1
    if i == 0:
        return fragment
    return fragment[i:].lstrip(" ")


def _line_fragment_pairs(src_line: str, tgt_line: str):
    src, tgt = src_line.strip(), tgt_line.strip()
    if not src or not tgt:
        return
    if _MD_SKIP_LINE_RE.match(src):
        return
    if src.startswith("|"):
        s_cells = mdblocks._CELL_SPLIT_RE.split(src.strip("|"))
        t_cells = mdblocks._CELL_SPLIT_RE.split(tgt.strip("|"))
        if len(s_cells) == len(t_cells):
            for sc, tc in zip(s_cells, t_cells):
                if sc.strip() and tc.strip():
                    yield sc.strip(), tc.strip()
        return
    for pat in _MD_LINE_PREFIX_RES:
        m = pat.match(src)
        if m:
            src = src[m.end():]
            m2 = pat.match(tgt)
            tgt = tgt[m2.end():] if m2 else tgt
            break
    if src and tgt:
        yield src, tgt


def build_translation_cache(paths: list[Path]) -> dict[str, str]:
    cache: dict[str, str] = {}
    for path in paths:
        entry = load_entry(path)
        if not entry:
            continue
        smd = entry.get("source_markdown") or ""
        tmd = entry.get("translated_markdown") or ""
        s_lines, t_lines = smd.split("\n"), tmd.split("\n")
        if len(s_lines) != len(t_lines):
            continue
        for s_line, t_line in zip(s_lines, t_lines):
            for s_text, t_text in _line_fragment_pairs(s_line, t_line):
                key = _md_inline_to_html(s_text)
                val = _md_inline_to_html(t_text)
                if not key:
                    continue
                cache.setdefault(key, val)
                alias = _strip_leading_symbols(key)
                val_alias = _strip_leading_symbols(val)
                if alias != key and val_alias != val:
                    cache.setdefault(alias, val_alias)
    return cache


# --------------------------------------------------------------------------- #
# State & storage
# --------------------------------------------------------------------------- #
def load_entry(path: Path) -> dict | None:
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return None


def save_entry(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(entry, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def blocks_plaintext(blocks: list[dict], span_key: str = "spans") -> str:
    lines = []
    for b in blocks:
        if b["type"] != "divider":
            text = "".join(s.get("text", "") for s in b.get(span_key, []))
            if text.strip():
                lines.append(text)
        for row in b.get("rows") or []:
            cells = ["".join(s.get("text", "")
                             for s in (c.get(span_key) or c.get("spans") or []))
                     for c in row]
            if any(c.strip() for c in cells):
                lines.append(" | ".join(cells))
        if b.get("children"):
            nested = blocks_plaintext(b["children"], span_key)
            if nested:
                lines.append(nested)
    return "\n".join(lines)


def needs_translation(entry: dict | None, page_hash: str) -> bool:
    if entry is None:
        return True
    if entry.get("schema_version") != SCHEMA_VERSION:
        return True
    if entry.get("translation_incomplete"):
        return True  # retry until the DeepL quota lets it finish
    return entry.get("content_hash") != page_hash


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    api_key = os.environ.get("DEEPL_API_KEY", "").strip()
    if not api_key:
        print("DEEPL_API_KEY is not set — skipping translation.", file=sys.stderr)
        return 0
    if not STATE_PATH.exists():
        print("snapshots/state.json not found — run monitor.py first. Skipping.")
        return 0

    tcfg = load_config()
    budget = int(os.environ.get("TRANSLATE_CHAR_BUDGET") or
                 tcfg["char_budget_per_run"])

    with open(STATE_PATH, "r", encoding="utf-8") as fh:
        pages: dict[str, dict] = json.load(fh).get("pages", {})

    base = deepl_base_url(api_key)
    session = requests.Session()
    session.headers.update({"Authorization": f"DeepL-Auth-Key {api_key}"})

    fetch_session = requests.Session()
    fetch_session.headers.update({"User-Agent": tcfg["user_agent"]})

    usage = deepl_usage(session, base)
    if usage:
        print(f"DeepL usage: {usage.get('character_count', '?')} / "
              f"{usage.get('character_limit', '?')} chars this period")

    eligible = {u: p for u, p in sorted(pages.items()) if is_eligible(u, p, tcfg)}
    print(f"{len(eligible)} eligible page(s); per-run budget {budget} chars.")

    cache = build_translation_cache(sorted(TRANSLATIONS_DIR.glob("*.json")))
    print(f"Reuse cache: {len(cache)} previously translated fragment(s).")

    done = skipped = deferred = incomplete_count = 0
    spent = 0
    budget_left = budget
    now = datetime.now(timezone.utc).isoformat()

    for url, page in eligible.items():
        path = TRANSLATIONS_DIR / f"{slug_for_url(url)}.json"
        entry = load_entry(path)
        if not needs_translation(entry, page["hash"]):
            skipped += 1
            continue

        try:
            resp = fetch_session.get(url, timeout=tcfg["request_timeout"])
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"  ! fetch failed, skipping: {url} -> {exc}", file=sys.stderr)
            continue

        source_blocks = html_to_blocks(resp.text, url)
        if not source_blocks:
            print(f"  ! no content blocks extracted, skipping: {url}",
                  file=sys.stderr)
            continue
        cover_url = extract_cover_url(resp.text)

        try:
            translated_blocks, sent, untranslated = translate_blocks(
                session, base, source_blocks, tcfg["target_lang"],
                tcfg["request_interval_seconds"], cache, budget_left)
        except PageBudgetExceeded as exc:
            print(f"  deferring (needs {exc} new chars, {budget_left} budget "
                  f"left): {url}")
            deferred += 1
            continue
        except requests.RequestException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (401, 403):
                print(f"DeepL auth failed ({status}) — check DEEPL_API_KEY.",
                      file=sys.stderr)
                return 1
            print(f"  ! translation failed, skipping: {url} -> {exc}",
                  file=sys.stderr)
            continue

        budget_left -= sent
        spent += sent
        incomplete = untranslated > 0
        if incomplete and entry is None:
            # A page never translated before: don't create a Notion page
            # that is (almost) entirely source-language — wait for quota.
            print(f"  deferring (quota exhausted, {untranslated} fragment(s) "
                  f"would stay untranslated): {url}")
            deferred += 1
            continue
        if incomplete:
            incomplete_count += 1
            print(f"  translated with {untranslated} fragment(s) left in the "
                  f"source language (DeepL quota exhausted): {url}")
        else:
            print(f"  translated ({sent} new chars, rest from cache): {url}")
        done += 1

        previous = entry or {}
        new_entry = {
            "schema_version": SCHEMA_VERSION,
            "url": url,
            "title": page.get("title", url),
            "content_hash": page["hash"],
            "target_lang": tcfg["target_lang"],
            "translator": "deepl",
            "translated_at": now,
            "source_markdown": mdblocks.blocks_to_markdown(source_blocks, "spans"),
            "translated_markdown": mdblocks.blocks_to_markdown(translated_blocks, "spans"),
            "source_text": blocks_plaintext(source_blocks),
            "translated_text": blocks_plaintext(translated_blocks),
            "review": {"status": "unreviewed", "reviewed_at": None,
                       "reviewer": None, "notes": ""},
            "notion": {
                "page_id": previous.get("notion", {}).get("page_id"),
                "synced_at": previous.get("notion", {}).get("synced_at"),
                "synced_hash": None,
            },
        }
        if cover_url:
            new_entry["cover_url"] = cover_url
        if incomplete:
            new_entry["translation_incomplete"] = True
        # Incomplete entries are retried every run until the quota returns;
        # don't churn the repo (or Notion) when nothing actually improved.
        if (incomplete and previous.get("translation_incomplete")
                and previous.get("translated_markdown")
                    == new_entry["translated_markdown"]
                and previous.get("source_markdown")
                    == new_entry["source_markdown"]):
            done -= 1
            incomplete_count -= 1
            skipped += 1
            continue
        save_entry(path, new_entry)

    print(f"Translated {done} page(s) ({spent} chars sent to DeepL, "
          f"{incomplete_count} partially), {deferred} deferred by budget, "
          f"{skipped} already up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
