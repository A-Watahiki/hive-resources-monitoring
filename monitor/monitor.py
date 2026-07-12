#!/usr/bin/env python3
"""
Hive Resource Library monitor.

Crawls the Hive Resource Library (and pages under it), stores a snapshot of
each page's visible text, compares against the previously stored snapshot, and
records anything added, removed, or changed in ``snapshots/updates.json`` —
the update history that notion_sync.py then renders as an "updates" section
on the Notion top page. No email (and no SMTP credentials) involved.

Design goals
------------
* Zero Claude / LLM usage at runtime. This is plain Python; a scheduled run
  consumes no AI tokens at all.
* State (the previous snapshot) is stored in ``snapshots/state.json`` and
  committed back to the repository by the GitHub Actions workflow, so each run
  can diff against the last one.

Configuration comes from ``monitor/config.yaml`` (crawl targets).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

import translate
from locales import get_locale

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "monitor" / "config.yaml"
STATE_PATH = ROOT / "snapshots" / "state.json"
UPDATES_PATH = ROOT / "snapshots" / "updates.json"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; HiveResourceMonitor/1.0; "
    "+https://github.com/a-watahiki/hive-resources-monitoring)"
)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    cfg.setdefault("start_urls", ["https://resources.joinhive.org/library"])
    cfg.setdefault("allowed_prefixes", ["https://resources.joinhive.org/"])
    cfg.setdefault("max_depth", 2)
    cfg.setdefault("max_pages", 300)
    cfg.setdefault("request_timeout", 30)
    cfg.setdefault("crawl_delay_seconds", 1.0)
    cfg.setdefault("user_agent", DEFAULT_USER_AGENT)
    cfg.setdefault("sitemap_urls", [])
    cfg.setdefault("ignore_url_patterns", [])
    cfg.setdefault("language", "en")
    cfg.setdefault("updates_keep", 10)
    return cfg


# --------------------------------------------------------------------------- #
# Fetching & parsing
# --------------------------------------------------------------------------- #
def normalize_url(url: str) -> str:
    """Drop the #fragment and trailing slash so the same page maps to one key."""
    url, _frag = urldefrag(url)
    if url.endswith("/") and len(urlparse(url).path) > 1:
        url = url.rstrip("/")
    return url


def is_allowed(url: str, cfg: dict) -> bool:
    if not any(url.startswith(p) for p in cfg["allowed_prefixes"]):
        return False
    for pat in cfg["ignore_url_patterns"]:
        if re.search(pat, url):
            return False
    return True


def extract_text(soup: BeautifulSoup) -> str:
    """Return normalized visible text, stripping non-content elements."""
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def extract_links(soup: BeautifulSoup, base_url: str, cfg: dict) -> set[str]:
    links: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        absolute = normalize_url(urljoin(base_url, href))
        if is_allowed(absolute, cfg):
            links.add(absolute)
    return links


def fetch(url: str, session: requests.Session, cfg: dict) -> requests.Response | None:
    try:
        resp = session.get(url, timeout=cfg["request_timeout"])
        resp.raise_for_status()
        return resp
    except requests.RequestException as exc:  # network error, 4xx, 5xx
        print(f"  ! fetch failed: {url} -> {exc}", file=sys.stderr)
        return None


def seed_from_sitemaps(session: requests.Session, cfg: dict) -> set[str]:
    """Optionally pull URLs from sitemap.xml files listed in the config."""
    found: set[str] = set()
    for sm in cfg["sitemap_urls"]:
        resp = fetch(sm, session, cfg)
        if resp is None:
            continue
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", resp.text):
            u = normalize_url(loc)
            if is_allowed(u, cfg):
                found.add(u)
    return found


def crawl(cfg: dict) -> dict[str, dict]:
    """Breadth-first crawl. Returns url -> {title, hash, text}."""
    session = requests.Session()
    session.headers.update({"User-Agent": cfg["user_agent"]})

    pages: dict[str, dict] = {}
    queue: deque[tuple[str, int]] = deque()
    seen: set[str] = set()

    for url in cfg["start_urls"]:
        u = normalize_url(url)
        queue.append((u, 0))
        seen.add(u)

    for url in seed_from_sitemaps(session, cfg):
        if url not in seen:
            queue.append((url, 1))
            seen.add(url)

    while queue and len(pages) < cfg["max_pages"]:
        url, depth = queue.popleft()
        print(f"  fetching (depth {depth}): {url}")
        resp = fetch(url, session, cfg)
        if resp is None:
            continue

        content_type = resp.headers.get("Content-Type", "")
        if "html" not in content_type.lower():
            # Non-HTML resource (PDF, image...). Track by content hash only.
            digest = hashlib.sha256(resp.content).hexdigest()
            pages[url] = {"title": os.path.basename(urlparse(url).path) or url,
                          "hash": digest, "text": f"[binary resource {content_type}]"}
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        title = (soup.title.string.strip() if soup.title and soup.title.string else url)
        text = extract_text(soup)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        pages[url] = {"title": title, "hash": digest, "text": text}

        if depth < cfg["max_depth"]:
            for link in extract_links(soup, url, cfg):
                if link not in seen:
                    seen.add(link)
                    queue.append((link, depth + 1))

        time.sleep(cfg["crawl_delay_seconds"])

    return pages


# --------------------------------------------------------------------------- #
# State & diffing
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return {"generated_at": None, "pages": {}}


def save_state(pages: dict[str, dict]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pages": pages,
    }
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)


def diff_pages(old: dict[str, dict], new: dict[str, dict]) -> dict:
    old_urls = set(old)
    new_urls = set(new)

    added = sorted(new_urls - old_urls)
    removed = sorted(old_urls - new_urls)
    changed = []
    for url in sorted(old_urls & new_urls):
        if old[url].get("hash") != new[url].get("hash"):
            text_diff = "\n".join(
                difflib.unified_diff(
                    old[url].get("text", "").splitlines(),
                    new[url].get("text", "").splitlines(),
                    fromfile="before",
                    tofile="after",
                    lineterm="",
                    n=2,
                )
            )
            changed.append({"url": url, "title": new[url].get("title", url),
                            "diff": text_diff})
    return {"added": added, "removed": removed, "changed": changed}


def has_changes(diff: dict) -> bool:
    return bool(diff["added"] or diff["removed"] or diff["changed"])


# --------------------------------------------------------------------------- #
# Update history (rendered onto the Notion top page by notion_sync.py)
# --------------------------------------------------------------------------- #
def load_updates() -> list[dict]:
    if UPDATES_PATH.exists():
        with open(UPDATES_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh).get("entries", [])
    return []


def save_updates(entries: list[dict], keep: int) -> None:
    UPDATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(UPDATES_PATH, "w", encoding="utf-8") as fh:
        json.dump({"entries": entries[-keep:]}, fh, ensure_ascii=False,
                  indent=2, sort_keys=True)
        fh.write("\n")


def build_update_entry(diff: dict, old_pages: dict, new_pages: dict,
                       max_diff_lines: int, tcfg: dict) -> dict:
    """One updates.json entry for this run's diff. Diff text is capped at
    max_diff_lines per changed page (the omitted count is kept so the
    rendering can say so)."""
    entry: dict = {
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "added": [{"url": u, "title": new_pages.get(u, {}).get("title", u)}
                  for u in diff["added"]],
        "removed": [{"url": u, "title": old_pages.get(u, {}).get("title", u)}
                    for u in diff["removed"]],
        "changed": [],
    }
    for item in diff["changed"]:
        lines = item["diff"].splitlines()
        entry["changed"].append({
            "url": item["url"],
            "title": item["title"],
            "diff": "\n".join(lines[:max_diff_lines]),
            "diff_lines_omitted": max(0, len(lines) - max_diff_lines),
            "translation_pending": translate.url_in_scope(item["url"], tcfg),
        })
    return entry


def format_update_entry(entry: dict, language: str = "en") -> str:
    """Plain-text rendering of an update entry for the run log."""
    msg = get_locale(language)
    parts: list[str] = []
    if entry["added"]:
        parts.append("■ " + msg["updates_added"].format(n=len(entry["added"])))
        parts += [f"  + {p['title']}\n    {p['url']}" for p in entry["added"]]
    if entry["removed"]:
        parts.append("■ " + msg["updates_removed"].format(n=len(entry["removed"])))
        parts += [f"  - {p['url']}" for p in entry["removed"]]
    if entry["changed"]:
        parts.append("■ " + msg["updates_changed"].format(n=len(entry["changed"])))
        for p in entry["changed"]:
            parts.append(f"\n● {p['title']}\n  {p['url']}")
            parts.append("  " + p["diff"].replace("\n", "\n  "))
            if p["diff_lines_omitted"]:
                parts.append("  " + msg["updates_diff_truncated"].format(
                    n=p["diff_lines_omitted"]))
            if p["translation_pending"]:
                parts.append("  " + msg["updates_translation_pending"])
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    cfg = load_config()
    dry_run = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

    print("Crawling Hive Resource Library ...")
    new_pages = crawl(cfg)
    print(f"Crawled {len(new_pages)} page(s).")

    if not new_pages:
        print("No pages fetched (site unreachable or blocked). Aborting without "
              "overwriting state.", file=sys.stderr)
        return 1

    state = load_state()
    old_pages = state.get("pages", {})
    first_run = not old_pages

    diff = diff_pages(old_pages, new_pages)

    if first_run:
        print("First run: recording baseline snapshot, no update recorded.")
        save_state(new_pages)
        return 0

    if not has_changes(diff):
        print("No changes detected.")
        save_state(new_pages)  # refresh generated_at timestamp
        return 0

    tcfg = translate.load_config()
    entry = build_update_entry(diff, old_pages, new_pages,
                               cfg.get("max_diff_lines", 200), tcfg)

    print("Changes detected:")
    print(f"  added={len(diff['added'])} removed={len(diff['removed'])} "
          f"changed={len(diff['changed'])}")
    print(format_update_entry(entry, cfg["language"]))

    if dry_run:
        print("--- DRY_RUN: update not recorded, state not saved ---")
        return 0

    save_updates(load_updates() + [entry], cfg["updates_keep"])
    save_state(new_pages)
    print(f"Update recorded in {UPDATES_PATH.relative_to(ROOT)}; it will "
          "appear on the Notion top page on the next sync.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
