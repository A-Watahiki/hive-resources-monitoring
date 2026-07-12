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

Before pushing anything, this also pulls the other direction: for each
already-synced page, it checks whether the live Notion content still
matches what the repo has, and if a human edited the translation directly
in the Notion UI, that edit is written back into the corresponding
translations/pages/<page>.json (see pull_manual_edits()) so the repo stays
the source of truth and the push below doesn't clobber it.

The top (parent) page also gets an "updates" callout box rendered from
``snapshots/updates.json`` (written by monitor.py), listing what was added,
removed, or changed on the original site per monitoring run — the
replacement for the old email notification (see sync_updates_section()).

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
from locales import get_locale

ROOT = Path(__file__).resolve().parent.parent
TRANSLATIONS_DIR = ROOT / "translations" / "pages"
UPDATES_PATH = ROOT / "snapshots" / "updates.json"

# The updates box on the top page is identified by this callout icon, so a
# re-run finds and refreshes the existing box instead of adding another.
UPDATES_ICON = "📢"

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
REQUEST_INTERVAL = 0.35  # Notion allows ~3 requests/second.

# Bump when how a page is *rendered* changes (title/icon derivation, block
# construction) even though the stored translation content didn't — forces
# every already-synced page to be re-pushed to Notion once, without needing
# a DeepL re-translation (content_hash / SCHEMA_VERSION are untouched).
RENDER_VERSION = 2

REVIEW_LABEL_KEYS = {
    "unreviewed": "review_unreviewed",
    "reviewed": "review_reviewed",
    "fixed": "review_fixed",
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


def header_blocks(entry: dict, language: str = "en") -> list[dict]:
    msg = get_locale(language)
    status = entry.get("review", {}).get("status", "unreviewed")
    label = msg.get(REVIEW_LABEL_KEYS.get(status, ""), status)
    translated_at = entry.get("translated_at", "")
    header = msg["notion_status_callout"].format(label=label,
                                                  translated_at=translated_at)
    return [
        {"object": "block", "type": "callout",
         "callout": {"icon": {"type": "emoji", "emoji": "🌐"},
                     "rich_text": [rich_text(header)]}},
        {"object": "block", "type": "paragraph",
         "paragraph": {"rich_text": [rich_text(msg["notion_source_label"]),
                                     rich_text(entry["url"], entry["url"])]}},
        {"object": "block", "type": "divider", "divider": {}},
    ]


def notion_page_url(page_id: str) -> str:
    return f"https://www.notion.so/{page_id.replace('-', '')}"


_LINK_URL_RE = re.compile(r"\]\(([^)]*)\)")


def rewrite_markdown_links(md_text: str, url_map: dict[str, str],
                           tcfg: dict) -> str:
    """Point [text](url) at the translated Notion page when url has one;
    otherwise, if url is a Hive resource we'd translate but haven't yet,
    leave it pointing at the original page and flag it as untranslated."""
    suffix = get_locale(tcfg.get("language"))["untranslated_suffix"]

    def repl(m: re.Match) -> str:
        url = m.group(1)
        if url in url_map:
            return f"]({url_map[url]})"
        if translate.url_in_scope(url, tcfg):
            return f"]({url}) {suffix}"
        return m.group(0)

    return _LINK_URL_RE.sub(repl, md_text)


_NON_CONTENT_BLOCK_TYPES = ("divider", "child_page", "child_database",
                           "link_to_page", "image", "table", "table_row",
                           "column_list", "column", "toggle", "callout")


def linkify_sole_links(blocks: list[dict],
                       page_id_by_notion_url: dict[str, str]) -> list[dict]:
    """Replace a block whose ENTIRE content is a single link to an
    already-translated resource with a native link_to_page block — the same
    "sub-page card" Notion renders for a real child page — instead of a
    plain-text hyperlink. Recurses into children (including the blocks
    inside a column layout). A block that mixes the link with other text
    (e.g. the "not yet translated" suffix) is left as-is."""
    out: list[dict] = []
    for b in blocks:
        btype = b.get("type")
        if btype not in _NON_CONTENT_BLOCK_TYPES and btype in b:
            rich = b[btype].get("rich_text") or []
            if (len(rich) == 1 and rich[0].get("type") == "text"
                    and rich[0]["text"].get("link")):
                page_id = page_id_by_notion_url.get(rich[0]["text"]["link"]["url"])
                if page_id:
                    out.append({"object": "block", "type": "link_to_page",
                               "link_to_page": {"type": "page_id",
                                                "page_id": page_id}})
                    continue
        nb = dict(b)
        if b.get("children"):
            nb["children"] = linkify_sole_links(b["children"], page_id_by_notion_url)
        if btype == "column_list" and b.get("column_list", {}).get("children"):
            nb["column_list"] = {"children": [
                dict(col, column={"children": linkify_sole_links(
                    col.get("column", {}).get("children") or [],
                    page_id_by_notion_url)})
                for col in b["column_list"]["children"]]}
        out.append(nb)
    return out


def content_blocks(entry: dict, url_map: dict[str, str],
                   page_id_map: dict[str, str], tcfg: dict) -> list[dict]:
    md = entry.get("translated_markdown")
    if md:
        blocks = mdblocks.markdown_to_notion_blocks(
            rewrite_markdown_links(md, url_map, tcfg))
        page_id_by_notion_url = {notion_page_url(pid): pid
                                 for pid in page_id_map.values()}
        return linkify_sole_links(blocks, page_id_by_notion_url)
    # Legacy fallback for entries stored before structured markdown existed.
    text = entry.get("translated_text", "")
    return [{"object": "block", "type": "paragraph",
             "paragraph": {"rich_text": [rich_text(line)]}}
            for line in text.split("\n") if line.strip()]


_WORD_RE = re.compile(r"\w", re.UNICODE)


def _block_plain_text(block: dict) -> str:
    btype = block.get("type")
    rich = (block.get(btype) or {}).get("rich_text") or []
    return "".join(r.get("text", {}).get("content", "")
                   for r in rich if r.get("type") == "text")


def _looks_like_page_icon(text: str) -> bool:
    """True for a short, word-free string like an emoji or emoji sequence —
    the Hive site's per-page icon, which shows up as its own leading
    paragraph ahead of the page's H1 in the extracted content."""
    t = text.strip()
    return (bool(t) and len(t) <= 8 and not _WORD_RE.search(t)
            and any(ord(c) >= 0x2000 for c in t))


def split_title_icon(blocks: list[dict]) -> tuple[str | None, str | None, list[dict]]:
    """Pull a leading "icon paragraph" + "# Title" heading pair off the
    front of the content blocks so the Hive page's own icon/title become
    the Notion page's icon/title instead of being duplicated in the body."""
    remaining = list(blocks)
    icon = None
    if remaining and remaining[0].get("type") == "paragraph":
        text = _block_plain_text(remaining[0])
        if _looks_like_page_icon(text):
            icon = text
            remaining = remaining[1:]
    title = None
    if remaining and remaining[0].get("type") == "heading_1":
        text = _block_plain_text(remaining[0]).strip()
        if text:
            title = text
            remaining = remaining[1:]
    return icon, title, remaining


def build_blocks(entry: dict, url_map: dict[str, str], page_id_map: dict[str, str],
                 tcfg: dict) -> tuple[str, str | None, str | None, list[dict]]:
    """Returns (title, icon_emoji_or_None, cover_url_or_None, blocks). The
    title/icon come from the translated content's own leading icon + heading
    when present, falling back to the (untranslated) title captured at crawl
    time; the cover is the original page's cover image, captured at
    translation time."""
    icon, translated_title, content = split_title_icon(
        content_blocks(entry, url_map, page_id_map, tcfg))
    title = translated_title or entry.get("title") or entry["url"]
    return (title, icon, entry.get("cover_url"),
            header_blocks(entry, tcfg.get("language")) + content)


# --------------------------------------------------------------------------- #
# The updates section on the top page (replaces the old email notification)
# --------------------------------------------------------------------------- #
def _bold_text(content: str, link: str | None = None) -> dict:
    item = rich_text(content, link)
    item["annotations"] = {"bold": True}
    return item


def _chunked_rich_text(text: str) -> list[dict]:
    """Split text into 2000-char rich_text items (Notion's per-item cap)."""
    return ([rich_text(text[i:i + mdblocks.RICH_TEXT_LIMIT])
             for i in range(0, len(text), mdblocks.RICH_TEXT_LIMIT)]
            or [rich_text("")])


def _updates_entry_title(entry: dict, msg: dict) -> str:
    n = len(entry["added"]) + len(entry["removed"]) + len(entry["changed"])
    return msg["updates_entry"].format(date=(entry.get("detected_at") or "")[:10],
                                       n=n)


def build_updates_children(entries: list[dict], url_map: dict[str, str],
                           msg: dict) -> list[dict]:
    """Blocks inside the updates callout: one collapsed toggle per monitor
    run that found changes (newest first), each listing the added / removed /
    changed pages. Page titles link to the translated Notion page when one
    exists, falling back to the original URL. A changed page also carries
    its (capped) text diff inside a nested "show diff" toggle."""
    def label(text: str) -> dict:
        return {"object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [_bold_text(text)]}}

    def bullet(rich: list[dict], children: list[dict] | None = None) -> dict:
        b: dict = {"object": "block", "type": "bulleted_list_item",
                   "bulleted_list_item": {"rich_text": rich}}
        if children:
            b["children"] = children
        return b

    out: list[dict] = []
    for entry in reversed(entries):  # newest first
        kids: list[dict] = []
        if entry.get("added"):
            kids.append(label(msg["updates_added"].format(n=len(entry["added"]))))
            for p in entry["added"]:
                kids.append(bullet([rich_text(p["title"],
                                              url_map.get(p["url"], p["url"]))]))
        if entry.get("removed"):
            kids.append(label(msg["updates_removed"].format(n=len(entry["removed"]))))
            for p in entry["removed"]:
                kids.append(bullet([rich_text(p.get("title") or p["url"])]))
        if entry.get("changed"):
            kids.append(label(msg["updates_changed"].format(n=len(entry["changed"]))))
            for p in entry["changed"]:
                inner: list[dict] = []
                diff_text = p.get("diff") or ""
                if p.get("diff_lines_omitted"):
                    diff_text += "\n" + msg["updates_diff_truncated"].format(
                        n=p["diff_lines_omitted"])
                if diff_text.strip():
                    inner.append({"object": "block", "type": "toggle",
                                  "toggle": {"rich_text":
                                             [rich_text(msg["updates_diff"])]},
                                  "children": [{"object": "block",
                                                "type": "paragraph",
                                                "paragraph": {"rich_text":
                                                              _chunked_rich_text(diff_text)}}]})
                if p.get("translation_pending"):
                    inner.append({"object": "block", "type": "paragraph",
                                  "paragraph": {"rich_text": [rich_text(
                                      msg["updates_translation_pending"])]}})
                kids.append(bullet([rich_text(p["title"],
                                              url_map.get(p["url"], p["url"]))],
                                   inner or None))
        out.append({"object": "block", "type": "toggle",
                    "toggle": {"rich_text": [_bold_text(_updates_entry_title(entry, msg))]},
                    "children": kids})
    return out


def sync_updates_section(notion: "Notion", parent_id: str,
                         url_map: dict[str, str], tcfg: dict,
                         refresh_links: bool = False) -> None:
    """Create or refresh the "updates" callout box on the top (parent) page
    from snapshots/updates.json — the replacement for the old email
    notification. The box is found again on later runs by its 📢 icon, so
    the user can drag it anywhere on the page; only its contents are
    replaced. When nothing changed since the last render (and no link
    upgrades are pending via refresh_links), the box is left untouched."""
    if not UPDATES_PATH.exists():
        return
    with open(UPDATES_PATH, "r", encoding="utf-8") as fh:
        entries = json.load(fh).get("entries", [])
    if not entries:
        return
    msg = get_locale(tcfg.get("language"))
    children = build_updates_children(entries, url_map, msg)

    try:
        box = next((b for b in notion.list_children(parent_id)
                    if b.get("type") == "callout"
                    and (b["callout"].get("icon") or {}).get("emoji") == UPDATES_ICON),
                   None)
        if box is None:
            notion.append_blocks(parent_id, [{
                "object": "block", "type": "callout",
                "callout": {"icon": {"type": "emoji", "emoji": UPDATES_ICON},
                            "color": "gray_background",
                            "rich_text": [_bold_text(msg["updates_heading"])]},
                "children": children}])
            print(f"  updates box created on the top page "
                  f"({len(entries)} entr{'y' if len(entries) == 1 else 'ies'}).")
            return
        live_kids = notion.list_children(box["id"])
        up_to_date = (len(live_kids) == len(children)
                      and live_kids
                      and live_kids[0].get("type") == "toggle"
                      and _block_plain_text(live_kids[0])
                      == _updates_entry_title(entries[-1], msg))
        if up_to_date and not refresh_links:
            return
        for b in live_kids:
            notion.request("DELETE", f"/blocks/{b['id']}").raise_for_status()
        notion.append_blocks(box["id"], children)
        print("  updates box refreshed on the top page.")
    except requests.RequestException as exc:
        body = getattr(exc.response, "text", "")[:300]
        print(f"  ! updates box sync failed, will retry next run: {exc} "
              f"| body: {body}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Pulling manual edits made directly in the Notion UI back into the repo
# --------------------------------------------------------------------------- #
_LIVE_TYPE_MAP = {
    "heading_1": ("heading", 1),
    "heading_2": ("heading", 2),
    "heading_3": ("heading", 3),
    "bulleted_list_item": ("bulleted", None),
    "numbered_list_item": ("numbered", None),
    "quote": ("quote", None),
    "paragraph": ("paragraph", None),
}


def _rich_text_to_spans(rich_text: list[dict]) -> list[dict]:
    """Notion silently merges adjacent same-style text runs when it stores a
    block (e.g. a dropped mailto: link leaves two plain-text runs on either
    side of what was a link, which come back as one on the next read), so
    this merges same-(href, bold) neighbors too — otherwise every such spot
    would look like a content difference to pull_manual_edits() even when
    nothing was actually edited."""
    spans: list[dict] = []
    for r in rich_text:
        if r.get("type") != "text":
            continue
        text_obj = r.get("text", {})
        text = text_obj.get("content", "")
        link = text_obj.get("link")
        href = link["url"] if link and link.get("url") else None
        bold = bool(r.get("annotations", {}).get("bold"))
        if spans and spans[-1].get("href") == href and bool(spans[-1].get("bold")) == bold:
            spans[-1]["text"] += text
            continue
        span = {"text": text}
        if href:
            span["href"] = href
        if bold:
            span["bold"] = True
        spans.append(span)
    return spans


def _live_block_to_internal(block: dict) -> dict | None:
    """The inverse of markdown_to_notion_blocks() + linkify_sole_links(), for
    a single live Notion block. Returns None for a block type this pipeline
    can't round-trip back to its markdown form (link_to_page, image, table,
    column layouts), which signals "skip this page" to the caller."""
    btype = block.get("type")
    if btype == "divider":
        return {"type": "divider"}
    if btype == "toggle":
        out: dict = {"type": "toggle",
                     "spans": _rich_text_to_spans(
                         (block.get("toggle") or {}).get("rich_text") or [])}
    elif btype == "callout":
        payload = block.get("callout") or {}
        icon = (payload.get("icon") or {}).get("emoji")
        out = {"type": "callout", "icon": icon,
               "color": payload.get("color") or "default",
               "spans": _rich_text_to_spans(payload.get("rich_text") or [])}
    elif btype in _LIVE_TYPE_MAP:
        kind, level = _LIVE_TYPE_MAP[btype]
        payload = block.get(btype) or {}
        out = {"type": kind,
               "spans": _rich_text_to_spans(payload.get("rich_text") or [])}
        if level:
            out["level"] = level
        if payload.get("is_toggleable"):
            out["toggle"] = True
    else:
        return None
    kids = block.get("children") or []
    if kids:
        inner = [_live_block_to_internal(c) for c in kids]
        if any(c is None for c in inner):
            return None
        out["children"] = inner
    return out


def _strip_header(blocks: list[dict]) -> list[dict]:
    """Remove the leading callout/link/divider header header_blocks() always
    prepends, matched by type so a page still round-trips even if a human
    reordered content after it."""
    i = 0
    if i < len(blocks) and blocks[i]["type"] == "callout":
        i += 1
    if i < len(blocks) and blocks[i]["type"] == "paragraph":
        i += 1
    if i < len(blocks) and blocks[i]["type"] == "divider":
        i += 1
    return blocks[i:]


def _blocks_structurally_match(a: list[dict], b: list[dict]) -> bool:
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if x["type"] != y["type"] or x.get("level") != y.get("level"):
            return False
        if not _blocks_structurally_match(x.get("children") or [],
                                          y.get("children") or []):
            return False
    return True


def _spans_differ(a: list[dict], b: list[dict]) -> bool:
    norm = lambda spans: [(s.get("text", ""), s.get("href"), bool(s.get("bold")))
                          for s in spans]
    return norm(a) != norm(b)


def _blocks_content_differs(a: list[dict], b: list[dict]) -> bool:
    """a and b are assumed structurally matching (see above); True if any
    block's text/link/bold content differs anywhere in the tree."""
    for x, y in zip(a, b):
        if x["type"] != "divider" and _spans_differ(x.get("spans", []), y.get("spans", [])):
            return True
        if _blocks_content_differs(x.get("children") or [], y.get("children") or []):
            return True
    return False


def pull_manual_edits(notion: "Notion", entries: dict[Path, dict],
                      url_map: dict[str, str], page_id_map: dict[str, str],
                      tcfg: dict) -> int:
    """Detect translations that were hand-edited directly on the Notion page
    (fixing a mistranslation there instead of in the repo) and pull that
    edit back into the corresponding translations/pages/<page>.json, so the
    repo stays the source of truth and the next sync doesn't clobber it.

    Only content-only edits are auto-pulled: the live page's block structure
    must exactly match what this pipeline would currently render (same
    block types/nesting, just different text) — anything else (blocks
    added/removed/reordered by hand, or a category-link page whose
    link_to_page cards can't be round-tripped) is left alone; edit
    translated_markdown in the repo directly for those instead."""
    pulled = 0
    for path, entry in entries.items():
        ninfo = entry.get("notion", {})
        page_id = ninfo.get("page_id")
        if not page_id or ninfo.get("synced_hash") != entry.get("content_hash"):
            continue  # nothing pushed yet this generation, or a push is pending
        icon, title, regenerated = split_title_icon(
            content_blocks(entry, url_map, page_id_map, tcfg))
        # regenerated is already in Notion API block shape (content_blocks()
        # built it that way) — normalize it through the same conversion as
        # the live page so the two sides are comparable.
        regenerated_internal = [_live_block_to_internal(b) for b in regenerated]
        if any(b is None for b in regenerated_internal):
            continue  # e.g. a category-link page (link_to_page) — skip
        try:
            live = notion.list_children_recursive(page_id)
        except requests.RequestException as exc:
            print(f"  ! pull check failed: {entry['url']} -> {exc}", file=sys.stderr)
            continue
        live = [b for b in _strip_header(live)
               if b["type"] not in ("child_page", "child_database")]
        live_internal = [_live_block_to_internal(b) for b in live]
        if any(b is None for b in live_internal):
            continue  # contains a block type we can't round-trip — skip
        regenerated = regenerated_internal
        if not _blocks_structurally_match(regenerated, live_internal):
            continue  # structure changed in Notion — needs a manual repo-side fix
        if not _blocks_content_differs(regenerated, live_internal):
            continue  # nothing to pull

        lead: list[dict] = []
        if icon:
            lead.append({"type": "paragraph", "spans": [{"text": icon}]})
        if title:
            lead.append({"type": "heading", "level": 1, "spans": [{"text": title}]})
        merged = lead + live_internal
        entry["translated_markdown"] = mdblocks.blocks_to_markdown(merged, "spans")
        entry["translated_text"] = translate.blocks_plaintext(merged)
        entry["review"] = {
            "status": "fixed",
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "reviewer": "notion-manual-edit",
            "notes": "Pulled a manual edit made directly on the Notion page.",
        }
        save_entry(path, entry)
        pulled += 1
        print(f"  pulled manual edit: {entry.get('title') or entry['url']}")
    return pulled


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

    def create_page(self, parent_id: str, title: str,
                    icon: str | None = None, cover: str | None = None) -> str:
        """Create an empty page and return its id — does NOT append content.
        Kept separate from filling in the content so a caller can persist
        the returned id before risking a failure in that (much more
        error-prone) step; otherwise a mid-append failure leaves a real,
        untracked page behind that the next run can't find and will
        recreate, producing an orphaned duplicate under the parent."""
        payload = {
            "parent": {"page_id": parent_id},
            "properties": {"title": {"title": [rich_text(title)]}},
        }
        if icon:
            payload["icon"] = {"type": "emoji", "emoji": icon}
        if cover:
            payload["cover"] = {"type": "external", "external": {"url": cover}}
        resp = self.request("POST", "/pages", json=payload)
        resp.raise_for_status()
        return resp.json()["id"]

    def append_blocks(self, parent_id: str, blocks: list[dict],
                      after: str | None = None) -> None:
        """Append `blocks` as children of parent_id (a page or block id).

        Notion's append endpoint rejects list items (and other block types)
        that carry a nested "children" array inline — it 400s with a
        validation error rather than creating the nested content. So each
        block is appended WITHOUT its children first, and once Notion
        returns that block's real id, its children are attached via a
        separate recursive call — this is the supported way to build
        nested structure through the public API.
        """
        flat: list[dict] = []
        nested: list[list[dict] | None] = []
        for b in blocks:
            nb = dict(b)
            nested.append(nb.pop("children", None))
            flat.append(nb)

        created_ids: list[str] = []
        for i in range(0, len(flat), 100):
            payload = {"children": flat[i:i + 100]}
            if after:
                payload["after"] = after
            resp = self.request("PATCH", f"/blocks/{parent_id}/children",
                                json=payload)
            resp.raise_for_status()
            results = resp.json().get("results") or []
            created_ids.extend(r["id"] for r in results)
            if results:
                after = results[-1]["id"]

        for block_id, kids in zip(created_ids, nested):
            if kids:
                self.append_blocks(block_id, kids)

    def set_title(self, page_id: str, title: str, icon: str | None = None,
                  cover: str | None = None) -> None:
        payload = {"properties": {"title": {"title": [rich_text(title)]}}}
        if icon:
            payload["icon"] = {"type": "emoji", "emoji": icon}
        if cover:
            payload["cover"] = {"type": "external", "external": {"url": cover}}
        resp = self.request("PATCH", f"/pages/{page_id}", json=payload)
        resp.raise_for_status()

    def clear_children(self, page_id: str) -> None:
        """Delete this page's content blocks — but NEVER child_page /
        child_database blocks. Those aren't inline content we rendered;
        each one *is* an actual nested sub-page, and deleting that block
        via the API archives the real page it represents. Skipping them
        means true Notion sub-pages survive a parent's content re-render."""
        cursor = None
        ids: list[str] = []
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            resp = self.request("GET", f"/blocks/{page_id}/children", params=params)
            resp.raise_for_status()
            data = resp.json()
            ids.extend(b["id"] for b in data["results"]
                      if b["type"] not in ("child_page", "child_database"))
            if not data.get("has_more"):
                break
            cursor = data["next_cursor"]
        for block_id in ids:
            self.request("DELETE", f"/blocks/{block_id}").raise_for_status()

    def replace_content_before_children(self, page_id: str,
                                        blocks: list[dict]) -> None:
        """Replace this page's own content, keeping it positioned BEFORE any
        real sub-pages (child_page/child_database blocks), which must stay
        last. clear-then-append would leave content stranded after those
        sub-pages: deleting the old content first collapses the child blocks
        to the top of the list, and a plain append then lands new content
        AFTER them. Instead, insert the new blocks at the old content's
        position first (immediately before the first child block), then
        delete the old content — so the final order is [content][children]
        regardless of API append-at-end behavior."""
        live = self.list_children(page_id)
        is_child = lambda b: b["type"] in ("child_page", "child_database")
        old_content_ids = [b["id"] for b in live if not is_child(b)]
        first_child_idx = next((i for i, b in enumerate(live) if is_child(b)), None)

        if first_child_idx is None:
            self.append_blocks(page_id, blocks)
        else:
            anchor = live[first_child_idx - 1]["id"] if first_child_idx > 0 else None
            self.append_blocks(page_id, blocks, after=anchor)

        for block_id in old_content_ids:
            self.request("DELETE", f"/blocks/{block_id}").raise_for_status()

    def page_exists(self, page_id: str) -> bool:
        resp = self.request("GET", f"/pages/{page_id}")
        return resp.status_code == 200 and not resp.json().get("archived", False)

    def list_children(self, page_id: str) -> list[dict]:
        cursor = None
        out: list[dict] = []
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            resp = self.request("GET", f"/blocks/{page_id}/children", params=params)
            resp.raise_for_status()
            data = resp.json()
            out.extend(data["results"])
            if not data.get("has_more"):
                break
            cursor = data["next_cursor"]
        return out

    def list_children_recursive(self, block_id: str) -> list[dict]:
        """list_children, but also fetches (and attaches as "children") the
        descendants of any block that has some, mirroring the nesting shape
        append_blocks() builds. Used to read back a page's live content for
        pull_manual_edits()."""
        blocks = self.list_children(block_id)
        for b in blocks:
            if b.get("has_children"):
                b["children"] = self.list_children_recursive(b["id"])
        return blocks

    def update_content_in_place(self, page_id: str, blocks: list[dict]) -> bool:
        """Rewrite content by PATCHing existing blocks in place, leaving every
        block (including child_page blocks and their position) where it is.
        Used for relinking: the block structure is unchanged, only some link
        URLs differ, so this avoids the clear+re-append that would shove real
        sub-pages to the top. Returns False if the live structure doesn't line
        up 1:1 with `blocks` (caller then falls back to clear+append)."""
        live = self.list_children(page_id)
        live_content = [b for b in live
                        if b["type"] not in ("child_page", "child_database")]
        if len(live_content) != len(blocks):
            return False
        for live_b, desired in zip(live_content, blocks):
            if live_b["type"] != desired["type"]:
                return False
        for live_b, desired in zip(live_content, blocks):
            t = desired["type"]
            if t == "divider" or "rich_text" not in desired.get(t, {}):
                continue
            resp = self.request("PATCH", f"/blocks/{live_b['id']}",
                                json={t: desired[t]})
            resp.raise_for_status()
        return True


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
    tcfg = translate.load_config()
    entries = load_all_entries(files)
    url_map = build_url_map(entries)
    page_id_map = build_page_id_map(entries)
    newly_available: set[str] = set()

    notion = Notion(token)

    if not files:
        print("No translations to sync yet.")
        sync_updates_section(notion, parent_id, url_map, tcfg)
        return 0

    pulled = pull_manual_edits(notion, entries, url_map, page_id_map, tcfg)

    synced = skipped = failed = deferred = 0

    for path, entry in entries.items():
        ninfo = entry.setdefault("notion", {})
        if (ninfo.get("synced_hash") == entry.get("content_hash")
                and ninfo.get("render_version") == RENDER_VERSION):
            skipped += 1
            continue

        page_id = ninfo.get("page_id")

        # Only decided at creation time — Notion has no API to move an
        # existing page, so once created its parent is fixed.
        target_parent = parent_id
        if not page_id:
            logical_parent = parent_url_of(entry["url"], tcfg)
            if logical_parent is not None:
                target_parent = page_id_map.get(logical_parent)
                if target_parent is None:
                    title = entry.get("title") or entry["url"]
                    print(f"  deferring (parent not translated yet): {title}")
                    deferred += 1
                    continue

        title, icon, cover, blocks = build_blocks(entry, url_map, page_id_map, tcfg)
        try:
            if page_id and not notion.page_exists(page_id):
                page_id = None  # was deleted in Notion; recreate below
            if not page_id:
                print(f"  creating: {title}")
                page_id = notion.create_page(target_parent, title, icon, cover)
                # Persist the id immediately: if appending content below
                # fails partway, the next run must find this (now real,
                # but still empty) page via page_id and finish it, rather
                # than creating a second page and leaving this one behind
                # as an orphaned duplicate under the parent.
                entry["notion"] = {
                    "page_id": page_id,
                    "synced_at": datetime.now(timezone.utc).isoformat(),
                    "synced_hash": None,
                    "render_version": RENDER_VERSION,
                }
                save_entry(path, entry)
                page_id_map[entry["url"]] = page_id
            else:
                print(f"  updating: {title}")
                notion.set_title(page_id, title, icon, cover)
            notion.replace_content_before_children(page_id, blocks)
        except requests.RequestException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (401, 403):
                print(f"Notion auth failed ({status}) — check NOTION_TOKEN and "
                      "that the parent page is shared with the integration.",
                      file=sys.stderr)
                return 1
            body = getattr(exc.response, "text", "")[:500]
            print(f"  ! sync failed, will retry next run: {entry['url']} -> {exc} "
                  f"| body: {body}", file=sys.stderr)
            failed += 1
            continue

        entry["notion"] = {
            "page_id": page_id,
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "synced_hash": entry["content_hash"],
            "render_version": RENDER_VERSION,
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
            try:
                title, _icon, _cover, blocks = build_blocks(entry, url_map,
                                                            page_id_map, tcfg)
                # Update in place so real sub-pages keep their position; only
                # fall back to a positional replace if the structure no
                # longer lines up.
                if not notion.update_content_in_place(page_id, blocks):
                    notion.replace_content_before_children(page_id, blocks)
            except requests.RequestException as exc:
                body = getattr(exc.response, "text", "")[:500]
                print(f"  ! relink failed, will retry next run: {entry['url']} "
                      f"-> {exc} | body: {body}", file=sys.stderr)
                continue
            print(f"  relinked: {title}")
            relinked += 1

    sync_updates_section(notion, parent_id, url_map, tcfg,
                         refresh_links=bool(newly_available))

    print(f"Notion sync: {pulled} pulled from Notion, {synced} synced, "
          f"{skipped} up to date, {failed} failed, {relinked} relinked, "
          f"{deferred} deferred (parent pending).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
