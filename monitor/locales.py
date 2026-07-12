#!/usr/bin/env python3
"""
Operator-facing message templates (the updates section on the Notion top
page, the Notion sync status callout, the "not yet translated" link
annotation), keyed by a locale code.

Everything else in the pipeline — crawling, diffing, DeepL translation,
Notion block construction — is language-agnostic. This is the only place
human-readable wording lives, so a fork only needs to touch this file (and
set `language` in config.yaml) to run in another language.

To add a language: copy the "en" block below, translate the values, and
add it under its locale code (lowercase). Placeholders like {n}, {date},
{label}, {translated_at} are filled in with `str.format()` — keep them
as-is, only translate the surrounding text.
"""

from __future__ import annotations

LOCALES: dict[str, dict[str, str]] = {
    "en": {
        "updates_heading": "Library updates (detected automatically)",
        "updates_entry": "{date}: {n} change(s)",
        "updates_added": "Added page(s) ({n})",
        "updates_removed": "Removed page(s) ({n})",
        "updates_changed": "Changed page(s) ({n})",
        "updates_diff": "Show diff",
        "updates_diff_truncated": "… ({n} more line(s) omitted)",
        "updates_translation_pending": "→ This page is translated; the "
                                       "translation will be updated to match "
                                       "on the next DeepL translation run.",
        "review_unreviewed": "unreviewed (raw DeepL machine translation)",
        "review_reviewed": "reviewed",
        "review_fixed": "reviewed (edited)",
        "notion_status_callout": "Status: {label} / Translated at: "
                                 "{translated_at} / This content is a "
                                 "DeepL machine translation",
        "notion_source_label": "Original page: ",
        "untranslated_suffix": "[not yet translated, links to the original]",
    },
    "ja": {
        "updates_heading": "更新情報（元サイトの変更を自動検出）",
        "updates_entry": "{date}：{n}件の変更",
        "updates_added": "追加されたページ（{n}件）",
        "updates_removed": "削除されたページ（{n}件）",
        "updates_changed": "内容が変更されたページ（{n}件）",
        "updates_diff": "差分を表示",
        "updates_diff_truncated": "…（差分が長いため {n} 行を省略）",
        "updates_translation_pending": "→ このページは翻訳対象です。次回のDeepL翻訳"
                                       "実行時に、変更を反映した翻訳に自動的に更新され"
                                       "ます。",
        "review_unreviewed": "未レビュー（DeepL自動翻訳のまま）",
        "review_reviewed": "レビュー済み",
        "review_fixed": "レビュー済み（修正あり）",
        "notion_status_callout": "ステータス: {label} ／ 翻訳日時: "
                                 "{translated_at} ／ この本文はDeepLによる"
                                 "機械翻訳です",
        "notion_source_label": "原文ページ: ",
        "untranslated_suffix": "[未翻訳, 元記事へのリンク]",
    },
}

DEFAULT_LOCALE = "en"


def get_locale(language: str | None) -> dict[str, str]:
    return LOCALES.get((language or DEFAULT_LOCALE).lower(), LOCALES[DEFAULT_LOCALE])
