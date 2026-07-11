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
SCHEMA_VERSION = 2

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
    return tcfg


def slug_for_url(url: str) -> str:
    path = urlparse(url).path.strip("/") or "root"
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", path)[:80]
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{digest}"


def is_eligible(url: str, page: dict, tcfg: dict) -> bool:
    if not any(url.startswith(p) for p in tcfg["include_prefixes"]):
        return False
    for pat in tcfg["exclude_url_patterns"]:
        if re.search(pat, url):
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


def _inline_spans(el, base_url: str) -> list[dict]:
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
                txt = c.get_text()
                if txt.strip():
                    span = {"text": txt, "bold": bold}
                    href = c.get("href")
                    if href and not href.startswith(("javascript:", "#")):
                        span["href"] = urljoin(base_url, href)
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


def _walk(node, base_url: str, blocks: list[dict]) -> None:
    for child in node.find_all(recursive=False):
        name = child.name
        if name in SKIP_TAGS or child.get("role") == "navigation":
            continue
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            spans = _inline_spans(child, base_url)
            if spans:
                blocks.append({"type": "heading", "level": min(int(name[1]), 3),
                               "spans": spans})
        elif name in ("ul", "ol"):
            kind = "numbered" if name == "ol" else "bulleted"
            for li in child.find_all("li", recursive=False):
                spans = _inline_spans(li, base_url)
                if spans:
                    blocks.append({"type": kind, "spans": spans})
                for sub in li.find_all(("ul", "ol"), recursive=False):
                    _walk_list(sub, base_url, blocks)
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
        elif _has_block_children(child):
            _walk(child, base_url, blocks)
        else:
            spans = _inline_spans(child, base_url)
            if spans:
                blocks.append({"type": "paragraph", "spans": spans})


def _walk_list(node, base_url: str, blocks: list[dict]) -> None:
    kind = "numbered" if node.name == "ol" else "bulleted"
    for li in node.find_all("li", recursive=False):
        spans = _inline_spans(li, base_url)
        if spans:
            blocks.append({"type": kind, "spans": spans})


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
                               interval: float) -> list[str]:
    results: list[str] = []
    for i in range(0, len(fragments), DEEPL_BATCH):
        chunk = fragments[i:i + DEEPL_BATCH]
        data = [("text", f) for f in chunk]
        data += [("target_lang", target_lang), ("tag_handling", "html")]
        resp = session.post(f"{base}/v2/translate", data=data, timeout=120)
        if resp.status_code == 456:
            raise DeepLQuotaExceeded("DeepL quota exhausted for this period")
        resp.raise_for_status()
        results.extend(t["text"] for t in resp.json()["translations"])
        time.sleep(interval)
    return results


def translate_blocks(session: requests.Session, base: str, blocks: list[dict],
                     target_lang: str, interval: float) -> list[dict]:
    idx = [i for i, b in enumerate(blocks) if b["type"] != "divider" and b.get("spans")]
    fragments = [spans_to_html(blocks[i]["spans"]) for i in idx]
    translated = deepl_translate_html_batch(session, base, fragments,
                                            target_lang, interval)
    out = [dict(b) for b in blocks]
    for i, frag in zip(idx, translated):
        out[i]["spans"] = html_to_spans(frag) or blocks[i]["spans"]
    return out


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
        if b["type"] == "divider":
            continue
        text = "".join(s.get("text", "") for s in b.get(span_key, []))
        if text.strip():
            lines.append(text)
    return "\n".join(lines)


def needs_translation(entry: dict | None, page_hash: str) -> bool:
    if entry is None:
        return True
    if entry.get("schema_version") != SCHEMA_VERSION:
        return True
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

    done = skipped = 0
    spent = 0
    now = datetime.now(timezone.utc).isoformat()

    for url, page in eligible.items():
        path = TRANSLATIONS_DIR / f"{slug_for_url(url)}.json"
        entry = load_entry(path)
        if not needs_translation(entry, page["hash"]):
            skipped += 1
            continue

        source_len = len(page.get("text", ""))
        if spent + source_len > budget and done > 0:
            print(f"Budget reached after {done} page(s); the rest will be "
                  "picked up on the next run.")
            break

        print(f"  translating ({source_len} chars): {url}")
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

        try:
            translated_blocks = translate_blocks(session, base, source_blocks,
                                                 tcfg["target_lang"],
                                                 tcfg["request_interval_seconds"])
        except DeepLQuotaExceeded:
            print("DeepL quota exhausted — stopping; progress so far is kept.",
                  file=sys.stderr)
            break
        except requests.RequestException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (401, 403):
                print(f"DeepL auth failed ({status}) — check DEEPL_API_KEY.",
                      file=sys.stderr)
                return 1
            print(f"  ! translation failed, skipping: {url} -> {exc}",
                  file=sys.stderr)
            continue

        spent += source_len
        done += 1

        previous = entry or {}
        save_entry(path, {
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
        })

    print(f"Translated {done} page(s) ({spent} chars), "
          f"{skipped} already up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
