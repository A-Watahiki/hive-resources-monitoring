#!/usr/bin/env python3
"""Throwaway diagnostic: dump the layout-relevant HTML structure of a few
Hive pages (toggles, callouts, tables, images, background colors) so the
extractor can be extended to preserve them. Run via inspect.yml, read the
log, then delete both files."""

from __future__ import annotations

import sys

import requests
from bs4 import BeautifulSoup

URLS = [
    "https://resources.joinhive.org/library",
    "https://resources.joinhive.org/library/ai-prompts",
    "https://resources.joinhive.org/library/community-building",
    "https://resources.joinhive.org/library/building-organisations",
    "https://resources.joinhive.org/library/building-organisations/executive-coaches",
]

INTERESTING = ("toggle", "callout", "column", "collapsible", "accordion",
               "bgcolor", "background")


def describe(el, depth=0):
    cls = " ".join(el.get("class") or [])
    txt = el.get_text()[:60].replace("\n", " ")
    print(f"{'  ' * depth}<{el.name} class='{cls}'> {txt!r}")


def main() -> int:
    sess = requests.Session()
    sess.headers["User-Agent"] = "Mozilla/5.0 (compatible; HiveResourceMonitor/1.0)"
    for url in URLS:
        print(f"\n{'=' * 70}\n{url}\n{'=' * 70}")
        html = sess.get(url, timeout=30).text
        soup = BeautifulSoup(html, "html.parser")

        print("\n-- <details>/<summary> elements --")
        for d in soup.find_all(["details", "summary"])[:10]:
            describe(d)
            for child in d.find_all(recursive=False)[:6]:
                describe(child, 1)

        print("\n-- elements with interesting class names --")
        seen = set()
        for el in soup.find_all(True):
            cls = " ".join(el.get("class") or [])
            if any(k in cls.lower() for k in INTERESTING):
                key = (el.name, cls)
                if key in seen:
                    continue
                seen.add(key)
                describe(el)
                for child in el.find_all(recursive=False)[:6]:
                    describe(child, 1)
                    for gc in child.find_all(recursive=False)[:4]:
                        describe(gc, 2)

        print("\n-- tables --")
        for t in soup.find_all("table")[:3]:
            describe(t)
            for tr in t.find_all("tr")[:3]:
                describe(tr, 1)
                for td in tr.find_all(["td", "th"])[:5]:
                    describe(td, 2)

        print("\n-- images (first 5, in main content) --")
        main_el = soup.find("main") or soup.find("article") or soup.body
        if main_el:
            for img in main_el.find_all("img")[:5]:
                print(f"  <img class='{' '.join(img.get('class') or [])}' "
                      f"src='{(img.get('src') or '')[:100]}' alt='{img.get('alt')}'>")

        print("\n-- distinct class names containing 'notion' (first 40) --")
        notion_classes = set()
        for el in soup.find_all(True):
            for c in el.get("class") or []:
                if "notion" in c.lower():
                    notion_classes.add(c)
        for c in sorted(notion_classes)[:40]:
            print(f"  {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
