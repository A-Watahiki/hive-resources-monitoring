#!/usr/bin/env python3
"""
DeepL translation pipeline for crawled Hive Resource Library pages.

Reads the crawl snapshot (``snapshots/state.json`` produced by monitor.py),
translates each eligible page's text to Japanese via the DeepL API, and stores
one JSON file per page under ``translations/pages/``.

Each JSON file carries everything needed downstream:

* ``source_text`` / ``translated_text`` — for the Claude review routine to
  check the translation against the original, page by page.
* ``review`` — review status, updated by the review routine.
* ``notion`` — sync bookkeeping used by notion_sync.py.

Translation is incremental and budgeted: pages are only (re)translated when
their content hash changes, and each run stops after ``char_budget_per_run``
source characters so the DeepL free tier (500k chars/month) is never blown in
one go. The backlog is worked through gradually across daily runs.

Environment:
    DEEPL_API_KEY          required. Keys ending in ":fx" use the free API host.
    TRANSLATE_CHAR_BUDGET  optional override of the per-run character budget.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "monitor" / "config.yaml"
STATE_PATH = ROOT / "snapshots" / "state.json"
TRANSLATIONS_DIR = ROOT / "translations" / "pages"

# Keep each DeepL request comfortably under the API's body-size limit.
CHUNK_CHARS = 4000


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    tcfg = cfg.get("translation") or {}
    tcfg.setdefault("include_prefixes", ["https://resources.joinhive.org/library"])
    tcfg.setdefault("exclude_url_patterns", [])
    tcfg.setdefault("target_lang", "JA")
    tcfg.setdefault("char_budget_per_run", 40000)
    tcfg.setdefault("min_text_length", 40)
    tcfg.setdefault("request_interval_seconds", 0.5)
    return tcfg


def slug_for_url(url: str) -> str:
    """Stable filesystem-safe name: sanitized path + short hash of the URL."""
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


def split_chunks(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Split on line boundaries so DeepL keeps context within paragraphs."""
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.split("\n"):
        # A single overlong line still has to fit somewhere.
        while len(line) > limit:
            if buf:
                chunks.append("\n".join(buf))
                buf, size = [], 0
            chunks.append(line[:limit])
            line = line[limit:]
        if size + len(line) + 1 > limit and buf:
            chunks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def deepl_translate(session: requests.Session, base: str, text: str,
                    target_lang: str, interval: float) -> str:
    translated: list[str] = []
    for chunk in split_chunks(text):
        resp = session.post(
            f"{base}/v2/translate",
            data={"text": chunk, "target_lang": target_lang},
            timeout=120,
        )
        if resp.status_code == 456:
            raise DeepLQuotaExceeded("DeepL quota exhausted for this period")
        resp.raise_for_status()
        translated.append(resp.json()["translations"][0]["text"])
        time.sleep(interval)
    return "\n".join(translated)


# --------------------------------------------------------------------------- #
# Main
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

    usage = deepl_usage(session, base)
    if usage:
        print(f"DeepL usage: {usage.get('character_count', '?')} / "
              f"{usage.get('character_limit', '?')} chars this period")

    eligible = {u: p for u, p in sorted(pages.items())
                if is_eligible(u, p, tcfg)}
    print(f"{len(eligible)} eligible page(s); per-run budget {budget} chars.")

    done = skipped = 0
    spent = 0
    now = datetime.now(timezone.utc).isoformat()

    for url, page in eligible.items():
        path = TRANSLATIONS_DIR / f"{slug_for_url(url)}.json"
        entry = load_entry(path)
        if entry and entry.get("content_hash") == page["hash"]:
            skipped += 1
            continue

        text = page["text"]
        if spent + len(text) > budget and done > 0:
            print(f"Budget reached after {done} page(s); the rest will be "
                  "picked up on the next run.")
            break

        print(f"  translating ({len(text)} chars): {url}")
        try:
            translated = deepl_translate(session, base, text,
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

        spent += len(text)
        done += 1

        previous = entry or {}
        save_entry(path, {
            "url": url,
            "title": page.get("title", url),
            "content_hash": page["hash"],
            "target_lang": tcfg["target_lang"],
            "translator": "deepl",
            "translated_at": now,
            "source_text": text,
            "translated_text": translated,
            # Content changed (or first translation): review starts over.
            "review": {"status": "unreviewed", "reviewed_at": None,
                       "reviewer": None, "notes": ""},
            # Keep the Notion page binding, but mark it stale for re-sync.
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
