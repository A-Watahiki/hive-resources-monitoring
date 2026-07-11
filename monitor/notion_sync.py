#!/usr/bin/env python3
"""
Sync Japanese translations to a personal Notion page.

For every JSON file under ``translations/pages/`` whose translation has not
yet been synced (``notion.synced_hash`` differs from ``content_hash``), this
script creates — or updates in place — a page under ``NOTION_PARENT_PAGE_ID``.
The translated content is rendered from the stored Markdown subset into
structured Notion blocks (headings, lists, quotes, dividers, links) so the
original Hive page layout is preserved.

New pages are nested as actual Notion sub-pages mirroring the Hive URL
structure (e.g. .../library/ai-prompts/academia is created as a child of the
already-translated .../library/ai-prompts page, which is itself a child of
.../library), rather than all being flat siblings under the top-level parent.
If a page's logical parent hasn't been translated yet, creation is deferred
(retried automatically on a later run) so it doesn't end up permanently
flat. Notion's API has no "move" operation, so this nesting is only decided
at creation time — pages that already exist keep their current parent.

Cross-links between resources (e.g. the library index linking to its category
pages) are rewritten to point at the corresponding translated Notion page
instead of the original Hive URL, whenever that target has already been
translated. Links to not-yet-translated pages are left pointing at the
original (live, readable) Hive page as a fallback. When a page is translated
for the first time, any already-synced page that links to it is re-rendered
("relinked") in the same run so the cross-link gets upgraded — without
needing to retranslate or re-detect a content change on the linking page.

Environment:
    NOTION_TOKEN           required. Internal-integration secret.
    NOTION_PARENT_PAGE_ID  required. The page under which entries are created
                           (the integration must have access to it).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import mdblocks
import translate

ROOT = Path(__file__).resolve().parent.parent
TRANSLATIONS_DIR = ROOT / "translations" / "pages"

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
REQUEST_INTERVAL = 0.35  # Notion allows ~3 requests/second.

REVIEW_LABELS = {
    "unreviewed": "未レビュー（DeepL自動翻訳のまま）",
    "reviewed": "レビュー済み",
    "fixed": "レビュー済み（修正あり）",
}


def normalize_page_id(raw: str) -> str:
    """Accept a bare 32-hex id, a dashed UUID, or a full Notion URL."""
    base = raw.split("?", 1)[0]
    runs = re.findall(r"[0-9a-f]{32,}", base.replace("-", "").lower())
    if not runs:
        raise ValueError(f"Could not find a Notion page id in: {raw!r}")
    h = runs[-1][-32:]
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def rich_text(content: str, link: str | None = None) -> dict:
    text: dict = {"content": content}
    if link:
        text["link"] = {"url": link}
    return {"type": "text", "text": text}


def header_blocks(entry: dict) -> list[dict]:
    status = entry.get("review", {}).get("status", "unreviewed")
    label = REVIEW_LABELS.get(status, status)
    translated_at = entry.get("translated_at", "")
    header = (f"ステータス: {label} ／ 翻訳日時: {translated_at} ／ "
              "この本文はDeepLによる機械翻訳です")
    return [
        {"object": "block", "type": "callout",
         "callout": {"icon": {"type": "emoji", "emoji": "🌐"},
                     "rich_text": [rich_text(header)]}},
        {"object": "block", "type": "paragraph",
         "paragraph": {"rich_text": [rich_text("原文ページ: "),
                                     rich_text(entry["url"], entry["url"])]}},
        {"object": "block", "type": "divider", "divider": {}},
    ]


def notion_page_url(page_id: str) -> str:
    return f"https://www.notion.so/{page_id.replace('-', '')}"


_LINK_URL_RE = re.compile(r"\]\(([^)]*)\)")

# Appended after a link that points at a Hive resource within our translation
# scope but hasn't been translated (and thus doesn't have a Notion page) yet.
UNTRANSLATED_SUFFIX = "[未翻訳, 元記事へのリンク]"


def rewrite_markdown_links(md_text: str, url_map: dict[str, str],
                           tcfg: dict) -> str:
    """Point [text](url) at the translated Notion page when url has one;
    otherwise, if url is a Hive resource we'd translate but haven't yet,
    leave it pointing at the original page and flag it as untranslated."""
    def repl(m: re.Match) -> str:
        url = m.group(1)
        if url in url_map:
            return f"]({url_map[url]})"
        if translate.url_in_scope(url, tcfg):
            return f"]({url}) {UNTRANSLATED_SUFFIX}"
        return m.group(0)

    return _LINK_URL_RE.sub(repl, md_text)


def content_blocks(entry: dict, url_map: dict[str, str], tcfg: dict) -> list[dict]:
    md = entry.get("translated_markdown")
    if md:
        return mdblocks.markdown_to_notion_blocks(
            rewrite_markdown_links(md, url_map, tcfg))
    # Legacy fallback for entries stored before structured markdown existed.
    text = entry.get("translated_text", "")
    return [{"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": [rich_text(line)]}}
            for line in text.split("\n") if line.strip()]


def build_blocks(entry: dict, url_map: dict[str, str], tcfg: dict) -> list[dict]:
    return header_blocks(entry) + content_blocks(entry, url_map, tcfg)


class Notion:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })

    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        resp = self.session.request(method, f"{NOTION_API}{path}",
                                    timeout=60, **kwargs)
        time.sleep(REQUEST_INTERVAL)
        if resp.status_code == 429:
            time.sleep(float(resp.headers.get("Retry-After", "2")))
            resp = self.session.request(method, f"{NOTION_API}{path}",
                                        timeout=60, **kwargs)
            time.sleep(REQUEST_INTERVAL)
        return resp

    def create_page(self, parent_id: str, title: str, blocks: list[dict]) -> str:
        resp = self.request("POST", "/pages", json={
            "parent": {"page_id": parent_id},
            "properties": {"title": {"title": [rich_text(title)]}},
            "children": blocks[:100],
        })
        resp.raise_for_status()
        page_id = resp.json()["id"]
        self.append_blocks(page_id, blocks[100:])
        return page_id

    def append_blocks(self, page_id: str, blocks: list[dict]) -> None:
        for i in range(0, len(blocks), 100):
            resp = self.request("PATCH", f"/blocks/{page_id}/children",
                                json={"children": blocks[i:i + 100]})
            resp.raise_for_status()

    def set_title(self, page_id: str, title: str) -> None:
        resp = self.request("PATCH", f"/pages/{page_id}", json={
            "properties": {"title": {"title": [rich_text(title)]}},
        })
        resp.raise_for_status()

    def clear_children(self, page_id: str) -> None:
        cursor = None
        ids: list[str] = []
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            resp = self.request("GET", f"/blocks/{page_id}/children", params=params)
            resp.raise_for_status()
            data = resp.json()
            ids.extend(b["id"] for b in data["results"])
            if not data.get("has_more"):
                break
            cursor = data["next_cursor"]
        for block_id in ids:
            self.request("DELETE", f"/blocks/{block_id}").raise_for_status()

    def page_exists(self, page_id: str) -> bool:
        resp = self.request("GET", f"/pages/{page_id}")
        return resp.status_code == 200 and not resp.json().get("archived", False)


def load_all_entries(files: list[Path]) -> dict[Path, dict]:
    entries = {}
    for path in files:
        with open(path, "r", encoding="utf-8") as fh:
            entries[path] = json.load(fh)
    return entries


def save_entry(path: Path, entry: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(entry, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def build_url_map(entries: dict[Path, dict]) -> dict[str, str]:
    """url -> already-synced Notion page URL, for cross-link rewriting."""
    url_map = {}
    for entry in entries.values():
        page_id = entry.get("notion", {}).get("page_id")
        if page_id:
            url_map[entry["url"]] = notion_page_url(page_id)
    return url_map


def build_page_id_map(entries: dict[Path, dict]) -> dict[str, str]:
    """url -> already-synced Notion page id (raw, for use as a parent)."""
    return {entry["url"]: entry["notion"]["page_id"]
            for entry in entries.values() if entry.get("notion", {}).get("page_id")}


def parent_url_of(url: str, tcfg: dict) -> str | None:
    """The logical parent resource URL (one path segment up within our
    translation scope), or None if url is already top-level."""
    scheme_host, _, path = url.partition("://")
    if not path:
        return None
    host, _, p = path.partition("/")
    p = "/" + p.rstrip("/")
    parent_path = p.rsplit("/", 1)[0]
    if not parent_path or parent_path == p:
        return None
    parent = f"{scheme_host}://{host}{parent_path}"
    return parent if translate.url_in_scope(parent, tcfg) else None


def main() -> int:
    token = os.environ.get("NOTION_TOKEN", "").strip()
    parent_raw = os.environ.get("NOTION_PARENT_PAGE_ID", "").strip()
    if not token or not parent_raw:
        print("NOTION_TOKEN / NOTION_PARENT_PAGE_ID not set — skipping Notion sync.")
        return 0
    parent_id = normalize_page_id(parent_raw)

    files = sorted(TRANSLATIONS_DIR.glob("*.json"))
    if not files:
        print("No translations to sync yet.")
        return 0

    tcfg = translate.load_config()
    entries = load_all_entries(files)
    url_map = build_url_map(entries)
    page_id_map = build_page_id_map(entries)
    newly_available: set[str] = set()

    notion = Notion(token)
    synced = skipped = failed = deferred = 0

    for path, entry in entries.items():
        ninfo = entry.setdefault("notion", {})
        if ninfo.get("synced_hash") == entry.get("content_hash"):
            skipped += 1
            continue

        title = entry.get("title") or entry["url"]
        page_id = ninfo.get("page_id")

        # Only decided at creation time — Notion has no API to move an
        # existing page, so once created its parent is fixed.
        target_parent = parent_id
        if not page_id:
            logical_parent = parent_url_of(entry["url"], tcfg)
            if logical_parent is not None:
                target_parent = page_id_map.get(logical_parent)
                if target_parent is None:
                    print(f"  deferring (parent not translated yet): {title}")
                    deferred += 1
                    continue

        blocks = build_blocks(entry, url_map, tcfg)
        try:
            if page_id and notion.page_exists(page_id):
                print(f"  updating: {title}")
                notion.set_title(page_id, title)
                notion.clear_children(page_id)
                notion.append_blocks(page_id, blocks)
            else:
                print(f"  creating: {title}")
                page_id = notion.create_page(target_parent, title, blocks)
        except requests.RequestException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (401, 403):
                print(f"Notion auth failed ({status}) — check NOTION_TOKEN and "
                      "that the parent page is shared with the integration.",
                      file=sys.stderr)
                return 1
            print(f"  ! sync failed, will retry next run: {entry['url']} -> {exc}",
                  file=sys.stderr)
            failed += 1
            continue

        entry["notion"] = {
            "page_id": page_id,
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "synced_hash": entry["content_hash"],
        }
        save_entry(path, entry)
        synced += 1

        page_id_map[entry["url"]] = page_id
        if entry["url"] not in url_map:
            newly_available.add(entry["url"])
        url_map[entry["url"]] = notion_page_url(page_id)

    # Pages that were skipped (their own content didn't change) may still
    # link to a page that only just got a Notion page in this run — refresh
    # their rendering so that cross-link points at the translation now.
    relinked = 0
    if newly_available:
        for path, entry in entries.items():
            ninfo = entry.get("notion", {})
            page_id = ninfo.get("page_id")
            if not page_id or ninfo.get("synced_hash") != entry.get("content_hash"):
                continue  # not yet synced, or already handled above
            md = entry.get("translated_markdown", "")
            if not any(u in md for u in newly_available):
                continue
            title = entry.get("title") or entry["url"]
            try:
                blocks = build_blocks(entry, url_map, tcfg)
                notion.clear_children(page_id)
                notion.append_blocks(page_id, blocks)
            except requests.RequestException as exc:
                print(f"  ! relink failed, will retry next run: {entry['url']} "
                      f"-> {exc}", file=sys.stderr)
                continue
            print(f"  relinked: {title}")
            relinked += 1

    print(f"Notion sync: {synced} synced, {skipped} up to date, {failed} failed, "
          f"{relinked} relinked, {deferred} deferred (parent pending).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
