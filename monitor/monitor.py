#!/usr/bin/env python3
"""
Hive Resource Library monitor.

Crawls the Hive Resource Library (and pages under it), stores a snapshot of
each page's visible text, compares against the previously stored snapshot, and
sends an email when anything is added, removed, or changed.

Design goals
------------
* Zero Claude / LLM usage at runtime. This is plain Python; a scheduled run
  consumes no AI tokens at all.
* State (the previous snapshot) is stored in ``snapshots/state.json`` and
  committed back to the repository by the GitHub Actions workflow, so each run
  can diff against the last one.

Configuration comes from ``monitor/config.yaml`` (crawl targets) and from
environment variables / GitHub Secrets (email credentials).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import smtplib
import sys
import time
from collections import deque
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

from locales import get_locale

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "monitor" / "config.yaml"
STATE_PATH = ROOT / "snapshots" / "state.json"

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
# Email
# --------------------------------------------------------------------------- #
def build_email_body(diff: dict, new_pages: dict, max_diff_lines: int,
                     language: str = "en") -> str:
    msg = get_locale(language)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts = [msg["email_intro"].format(now=now), ""]

    if diff["added"]:
        parts.append("■ " + msg["email_added"].format(n=len(diff["added"])))
        for url in diff["added"]:
            title = new_pages.get(url, {}).get("title", url)
            parts.append(f"  + {title}\n    {url}")
        parts.append("")

    if diff["removed"]:
        parts.append("■ " + msg["email_removed"].format(n=len(diff["removed"])))
        for url in diff["removed"]:
            parts.append(f"  - {url}")
        parts.append("")

    if diff["changed"]:
        parts.append("■ " + msg["email_changed"].format(n=len(diff["changed"])))
        for item in diff["changed"]:
            parts.append(f"\n● {item['title']}\n  {item['url']}")
            diff_lines = item["diff"].splitlines()
            if len(diff_lines) > max_diff_lines:
                diff_lines = diff_lines[:max_diff_lines] + [
                    msg["email_diff_truncated"].format(
                        n=len(diff_lines) - max_diff_lines)
                ]
            parts.append("  " + "\n  ".join(diff_lines))
        parts.append("")

    parts.append("---")
    parts.append(msg["email_footer"])
    return "\n".join(parts)


def send_email(subject: str, body: str) -> None:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    email_from = os.environ.get("EMAIL_FROM", user)
    email_to = [a.strip() for a in os.environ["EMAIL_TO"].split(",") if a.strip()]
    from_name = os.environ.get("EMAIL_FROM_NAME", "Hive Resource Monitor")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, email_from))
    msg["To"] = ", ".join(email_to)

    with smtplib.SMTP(host, port, timeout=60) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(email_from, email_to, msg.as_string())
    print(f"  email sent to {', '.join(email_to)}")


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
        print("First run: recording baseline snapshot, no email sent.")
        save_state(new_pages)
        return 0

    if not has_changes(diff):
        print("No changes detected.")
        save_state(new_pages)  # refresh generated_at timestamp
        return 0

    n = len(diff["added"]) + len(diff["removed"]) + len(diff["changed"])
    msg = get_locale(cfg["language"])
    subject = msg["email_subject"].format(n=n)
    body = build_email_body(diff, new_pages, cfg.get("max_diff_lines", 200),
                            cfg["language"])

    print("Changes detected:")
    print(f"  added={len(diff['added'])} removed={len(diff['removed'])} "
          f"changed={len(diff['changed'])}")

    if dry_run:
        print("--- DRY_RUN: email not sent, state not saved ---")
        print(body)
        return 0

    send_email(subject, body)
    save_state(new_pages)
    return 0


if __name__ == "__main__":
    sys.exit(main())
