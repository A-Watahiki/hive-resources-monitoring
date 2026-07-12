#!/usr/bin/env python3
"""
Operator-facing message templates (monitor emails, the Notion sync status
callout, the "not yet translated" link annotation), keyed by a locale code.

Everything else in the pipeline — crawling, diffing, DeepL translation,
Notion block construction — is language-agnostic. This is the only place
human-readable wording lives, so a fork only needs to touch this file (and
set `language` in config.yaml) to run in another language.

To add a language: copy the "en" block below, translate the values, and
add it under its locale code (lowercase). Placeholders like {n}, {now},
{label}, {translated_at} are filled in with `str.format()` — keep them
as-is, only translate the surrounding text.
"""

from __future__ import annotations

LOCALES: dict[str, dict[str, str]] = {
    "en": {
        "email_subject": "[Hive Library] Update detected ({n} change(s))",
        "email_intro": "Detected an update to the Hive Resource Library ({now})",
        "email_added": "Added page(s) ({n})",
        "email_removed": "Removed page(s) ({n})",
        "email_changed": "Changed page(s) ({n})",
        "email_diff_truncated": "  … ({n} more line(s) omitted)",
        "email_translation_pending": "  → This page is translated; the "
                                     "translation will be updated to match "
                                     "on the next DeepL translation run.",
        "email_footer": "This email was sent automatically by "
                        "hive-resources-monitoring (GitHub Actions).",
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
        "email_subject": "[Hive Library] 更新を検出（{n}件の変更）",
        "email_intro": "Hive Resource Library の更新を検出しました（{now}）",
        "email_added": "追加されたページ（{n}件）",
        "email_removed": "削除されたページ（{n}件）",
        "email_changed": "内容が変更されたページ（{n}件）",
        "email_diff_truncated": "  … （差分が長いため {n} 行を省略）",
        "email_translation_pending": "  → このページは翻訳対象です。次回のDeepL翻訳"
                                     "実行時に、修正を反映した翻訳に自動的に更新され"
                                     "ます。",
        "email_footer": "このメールは hive-resources-monitoring（GitHub Actions）"
                        "から自動送信されました。",
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
