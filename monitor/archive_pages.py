#!/usr/bin/env python3
"""
Throwaway cleanup: move specific Notion pages to the trash (archive them),
cascading to their sub-pages. Used once to remove the duplicate/orphan pages
left behind when translation entries were reset for a clean rebuild.

Usage:
    ARCHIVE_PAGE_IDS="id1,id2" NOTION_TOKEN=... python monitor/archive_pages.py
"""

from __future__ import annotations

import os
import sys

import requests

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def main() -> int:
    token = os.environ["NOTION_TOKEN"].strip()
    ids = [i.strip() for i in os.environ.get("ARCHIVE_PAGE_IDS", "").split(",")
           if i.strip()]
    if not ids:
        print("ARCHIVE_PAGE_IDS is empty — nothing to do.")
        return 0

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    })

    failed = 0
    for pid in ids:
        resp = session.patch(f"{NOTION_API}/pages/{pid}",
                             json={"in_trash": True}, timeout=60)
        if resp.status_code == 200:
            print(f"  archived: {pid}")
        else:
            print(f"  ! failed ({resp.status_code}): {pid} -> {resp.text[:200]}",
                  file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
