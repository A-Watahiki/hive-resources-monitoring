#!/usr/bin/env python3
"""
Sync Japanese translations to a personal Notion page.

For every JSON file under ``translations/pages/`` whose translation has not
yet been synced (``notion.synced_hash`` differs from ``content_hash``), this
script creates — or updates in place — a child page under the Notion parent
page given by ``NOTION_PARENT_PAGE_ID``. The translated content is rendered
from the stored Markdown subset into structured Notion blocks (headings,
lists, quotes, dividers, links) so the original Hive page layout is preserved.

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


def content_blocks(entry: dict) -> list[dict]:
    md = entry.get("translated_markdown")
    if md:
        return mdblocks.markdown_to_notion_blocks(md)
    # Legacy fallback for entries stored before structured markdown existed.
    text = entry.get("translated_text", "")
    return [{"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": [rich_text(line)]}}
            for line in text.split("\n") if line.strip()]


def build_blocks(entry: dict) -> list[dict]:
    return header_blocks(entry) + content_blocks(entry)


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

    notion = Notion(token)
    synced = skipped = failed = 0

    for path in files:
        with open(path, "r", encoding="utf-8") as fh:
            entry = json.load(fh)

        ninfo = entry.setdefault("notion", {})
        if ninfo.get("synced_hash") == entry.get("content_hash"):
            skipped += 1
            continue

        title = entry.get("title") or entry["url"]
        blocks = build_blocks(entry)
        try:
            page_id = ninfo.get("page_id")
            if page_id and notion.page_exists(page_id):
                print(f"  updating: {title}")
                notion.set_title(page_id, title)
                notion.clear_children(page_id)
                notion.append_blocks(page_id, blocks)
            else:
                print(f"  creating: {title}")
                page_id = notion.create_page(parent_id, title, blocks)
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
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(entry, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        synced += 1

    print(f"Notion sync: {synced} synced, {skipped} up to date, {failed} failed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
