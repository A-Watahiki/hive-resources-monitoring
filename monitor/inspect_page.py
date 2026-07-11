#!/usr/bin/env python3
"""
Throwaway diagnostic: fetch a page and report its HTML structure so we can
tell why some resource pages (e.g. .../ai-prompts/advertisements) come out
with only their item titles and none of the prompt bodies.

Usage (on a machine that can reach the site, e.g. a GitHub Actions runner):
    INSPECT_URL="https://resources.joinhive.org/library/ai-prompts/advertisements" \
        python monitor/inspect_page.py
"""

from __future__ import annotations

import os
import re
import sys
from collections import Counter

import requests
from bs4 import BeautifulSoup

import translate

URL = os.environ.get("INSPECT_URL",
                     "https://resources.joinhive.org/library/ai-prompts/advertisements")


def main() -> int:
    tcfg = translate.load_config()
    sess = requests.Session()
    sess.headers.update({"User-Agent": tcfg["user_agent"]})
    resp = sess.get(URL, timeout=tcfg["request_timeout"])
    resp.raise_for_status()
    html = resp.text

    print(f"URL: {URL}")
    print(f"raw HTML length: {len(html)} chars")

    soup = BeautifulSoup(html, "html.parser")

    # 1. Tag histogram of the whole document.
    tags = Counter(t.name for t in soup.find_all())
    interesting = ["details", "summary", "li", "ul", "ol", "p", "h1", "h2",
                   "h3", "h4", "toggle", "blockquote", "pre", "code", "div",
                   "span", "script", "noscript", "template"]
    print("\n-- tag counts (interesting) --")
    for t in interesting:
        if tags.get(t):
            print(f"  {t}: {tags[t]}")

    # 2. Notion-ish class names (super.so / potion / notion render hints).
    classes = Counter()
    for el in soup.find_all(class_=True):
        for c in el.get("class", []):
            classes[c] += 1
    notionish = {c: n for c, n in classes.items()
                 if re.search(r"toggle|notion|collaps|accordion|detail", c, re.I)}
    print("\n-- toggle/notion-ish class names --")
    for c, n in sorted(notionish.items(), key=lambda x: -x[1])[:25]:
        print(f"  {c}: {n}")
    if not notionish:
        print("  (none)")

    # 3. What our current extractor produces.
    blocks = translate.html_to_blocks(html, URL)
    print(f"\n-- extractor output: {len(blocks)} block(s) --")
    for b in blocks[:40]:
        text = "".join(s.get("text", "") for s in b.get("spans", []))
        print(f"  [{b['type']}] {text[:100]}")

    # 4. Is there prompt-like body text in the raw HTML that we're dropping?
    # Compare total visible text length vs what we extracted.
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    visible = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    extracted = " ".join("".join(s.get("text", "") for s in b.get("spans", []))
                         for b in blocks)
    print(f"\n-- text volume --")
    print(f"  visible text in HTML: {len(visible)} chars")
    print(f"  text we extracted:    {len(extracted)} chars")

    # 5. Show a chunk of visible text so we can eyeball whether prompt bodies
    #    exist in the static HTML at all.
    print("\n-- first 1500 chars of visible text --")
    print(visible[:1500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
