#!/usr/bin/env python3
"""
Show translation/review progress, and list files awaiting review.

Used by humans and by the Claude review routine (/review-translations) to pick
the next few pages to check without loading everything into context.

Usage:
    python monitor/translation_status.py                 # summary
    python monitor/translation_status.py --list-unreviewed [N]
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRANSLATIONS_DIR = ROOT / "translations" / "pages"
STATE_PATH = ROOT / "snapshots" / "state.json"


def main() -> int:
    files = sorted(TRANSLATIONS_DIR.glob("*.json"))
    entries = []
    for path in files:
        with open(path, "r", encoding="utf-8") as fh:
            entries.append((path, json.load(fh)))

    if len(sys.argv) > 1 and sys.argv[1] == "--list-unreviewed":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        listed = 0
        for path, entry in entries:
            if entry.get("review", {}).get("status") == "unreviewed":
                print(f"{path.relative_to(ROOT)}\t{entry['url']}\t"
                      f"{len(entry.get('source_text', ''))} chars")
                listed += 1
                if listed >= limit:
                    break
        if listed == 0:
            print("No unreviewed translations. 🎉")
        return 0

    total_crawled = 0
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            total_crawled = len(json.load(fh).get("pages", {}))

    statuses = Counter(e.get("review", {}).get("status", "unreviewed")
                       for _, e in entries)
    synced = sum(1 for _, e in entries
                 if e.get("notion", {}).get("synced_hash")
                 and e["notion"]["synced_hash"] == e.get("content_hash"))
    chars = sum(len(e.get("source_text", "")) for _, e in entries)

    print(f"crawled pages (snapshot):   {total_crawled}")
    print(f"translated pages:           {len(entries)} ({chars} source chars)")
    for status, count in sorted(statuses.items()):
        print(f"  review {status:<12} {count}")
    print(f"synced to Notion (current): {synced}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
