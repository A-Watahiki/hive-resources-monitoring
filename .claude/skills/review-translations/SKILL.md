---
name: review-translations
description: Check DeepL-translated Hive Resource Library pages against their source, fix any issues found, and update the review status. Processes only a few pages per invocation to keep token usage low while gradually working through the backlog. Takes an optional page-count argument (e.g. /review-translations 5, default 3).
---

# Translation Review Routine

Checks the DeepL translations stored under `translations/pages/*.json` against
their source text. **Only a small number of pages are processed per
invocation** (default 3, or the number given as an argument) — the rest are
picked up gradually on later invocations.

## Steps

1. Get the pages due for review:

   ```bash
   python monitor/translation_status.py --list-unreviewed <N>
   ```

   Each output line is `file path, URL, source character count`. If there
   are none left unreviewed, tell the user and stop.

2. Read each file and compare `source_markdown` (original) against
   `translated_markdown` (DeepL output) for:
   - **Mistranslations / meaning reversal** (negation flipped, subject
     confused, etc.)
   - **Dropped content** (a paragraph or sentence present in the source but
     missing from the translation)
   - **Terminology consistency**: domain-specific terms (this project's is
     animal welfare/advocacy) should use the same rendering throughout —
     check `config.yaml`'s `translation.target_lang` for which language
     this deployment is translating into, and judge against that language's
     conventions. Organization names, people's names, and program names
     don't need to be forced into the target language.
   - **Navigation-derived noise** translating awkwardly is fine to leave as
     long as the actual body content reads correctly.

3. Only if a fix is needed, edit `translated_markdown` with Edit. Don't
   rewrite the whole thing — make a targeted fix at the problem spot only.
   **Preserve the Markdown syntax**: heading `#`/`##`/`###`, list markers
   `- `/`1. `, quote `> `, divider `---`, links `[text](URL)`, bold
   `**text**` — these drive how the page renders as Notion blocks, so don't
   break them. Don't change link URLs. It's good practice to also fix
   `translated_text` (the plain-text copy) to match, but Notion rendering
   uses `translated_markdown`.

4. Update each file's `review` object:
   - No fix needed → `"status": "reviewed"`
   - Fixed something → `"status": "fixed"`
   - Either way: set `"reviewed_at"` to the current time (UTC, ISO 8601),
     `"reviewer": "claude"`, and a one-line summary in `"notes"` (e.g.
     "fixed 2 mistranslations" / "no issues found").

   **Important**: setting `notion.synced_hash` to `null` makes the next
   Notion sync push the corrected translation to the live page. Always do
   this if you edited `translated_text`; leave it as-is if you made no fix.

5. Commit and push together:

   ```bash
   git add translations/ && git commit -m "review: translation check (N pages)" && git push
   ```

6. Report to the user: how many pages were checked, how many fixes were
   made, and the remaining unreviewed count (from
   `python monitor/translation_status.py`).
