# hive-resources-monitoring

Periodically monitors the [Hive Resource Library](https://resources.joinhive.org/library)
and shows what changed in an "updates" box on a Notion page. It also includes
a DeepL-powered translation pipeline that syncs translated pages to your own
Notion. Everything runs as plain Python on GitHub Actions — no AI tokens
consumed.

## 🔰 Quickstart (no programming experience required)

**You don't need a terminal, the command line, or any coding knowledge.**
Everything below is done through your browser, and produces a translated
copy of the library in your own language (about 15 minutes, plus automated
processing time).

1. **Fork this repository**
   Click **Fork** at the top right of this page to copy it into your own
   GitHub account.
2. **Get a free DeepL API key**
   Sign up at [DeepL API Free](https://www.deepl.com/pro-api) and copy your
   API key (a key ending in `:fx` is the free tier).
3. **Set up the Notion side**
   - At [notion.so/my-integrations](https://www.notion.so/my-integrations),
     create a "New integration" and copy the **token** it shows you
     (`ntn_...`).
   - Pick (or create) one Notion page to hold the translated pages, then
     from its **⋯ → Connections** menu, add the integration you just
     created (this shares the page with it).
   - Copy that page's URL from your browser — you'll use it as the page ID.
4. **Add the keys to your fork**
   In your forked repository, go to **Settings → Secrets and variables →
   Actions → New repository secret** and add these three, one at a time:

   | Secret name | Value |
   |---|---|
   | `DEEPL_API_KEY` | The DeepL API key from step 2 |
   | `NOTION_TOKEN` | The Notion token from step 3 |
   | `NOTION_PARENT_PAGE_ID` | The Notion page URL from step 3 |
5. **Give Actions write permission**
   Under **Settings → Actions → General → Workflow permissions**, select
   **"Read and write permissions"** and save.
6. **Set your target language**
   Open `monitor/config.yaml` in your browser and click the pencil icon
   (Edit this file) to edit it directly.
   - Change `language:` to your locale code (e.g. `"ko"`, `"fr"`)
   - Change `translation.target_lang:` to the matching
     [DeepL language code](https://developers.deepl.com/docs/getting-started/supported-languages)
     (e.g. `"KO"`, `"FR"`)
   - Save with "Commit changes" at the bottom of the page

   > If your language isn't `en` or `ja`, you'll also need to add a few
   > translated phrases to one more file (`monitor/locales.py`). See
   > "[Localization](#localization)" below for details — an AI assistant
   > can do this step for you too.
7. **Run it once manually to check it works**
   Open the **Actions** tab and run **"Monitor Hive Resource Library" → Run
   workflow** first. Once it finishes (a few minutes), run **"Translate &
   Sync to Notion" → Run workflow** (the first run can take tens of minutes).
8. **Check Notion**
   Open the Notion page from step 3 and watch translated pages appear. From
   here on, it runs automatically every day (crawl at 00:00 UTC, translate
   at 01:00 UTC — edit the `cron` schedule in `.github/workflows/*.yml` to
   change the timing).
9. **(Optional) Automate translation quality review with Claude**
   Machine translation isn't perfect. This repo includes a Claude Code
   skill, `/review-translations`
   ([`.claude/skills/review-translations/SKILL.md`](.claude/skills/review-translations/SKILL.md)),
   that checks a handful of translated pages against their source each time
   it runs, fixes anything it finds, and commits the result — so quality
   improves gradually without you checking every page by hand. Unlike the
   rest of the pipeline, this step does use Claude/AI usage.
   - Go to [claude.ai/code](https://claude.ai/code) (Claude Code on the
     web) and connect your forked repository as a source.
   - Start a session in that repo and ask Claude to set it up for you, e.g.:
     *"Set up a daily routine that runs `/review-translations 3` in this
     repo."* Claude will create the recurring schedule (a "Routine") for you.
   - Each run only checks a few pages (3 by default) to keep usage low; the
     backlog is worked through gradually across runs. Check progress
     anytime with `python monitor/translation_status.py`.

That's it. Everything below is reference material for how it works and how
to customize it further.

## How it works

```
GitHub Actions (daily, 00:00 UTC)
   └─ monitor.py    : crawls the library, diffs against the previous
                       snapshot, and records any changes in
                       snapshots/updates.json
GitHub Actions (daily, 01:00 UTC — an hour later)
   └─ translate.py   : translates new/changed pages via DeepL, saving
                        source + translation to translations/pages/*.json
   └─ notion_sync.py : pushes translations to Notion and refreshes the
                        📢 updates box
```

Detected changes: pages **added**, **removed**, or **changed** (a changed
page keeps a text diff, shown behind a "show diff" toggle in the updates
box).

## Features

- **Uses zero AI tokens.** Monitoring, translation, and Notion sync are all
  plain Python.
- Runs entirely within GitHub Actions' free tier — no email, no external
  services.
- The previous snapshot is stored in the repo itself
  (`snapshots/state.json`) and diffed against on the next run.

## Trying it locally (optional)

```bash
pip install -r monitor/requirements.txt
DRY_RUN=true python monitor/monitor.py
```

`DRY_RUN=true` just logs the detected diff — it doesn't touch the snapshot
or the update history.

## Translation & Notion sync, in detail

**Updates box**: `notion_sync.py` renders the history in
`snapshots/updates.json` as a 📢 callout on the Notion parent page. Each run
that found changes becomes one collapsed toggle (e.g. `2026-07-12: 3
change(s)`), with page titles linked to the translated Notion page (or the
original, if not yet translated). The box is created automatically on first
sync and can be moved anywhere on the page — it's found again by its 📢
icon. `updates_keep` in `config.yaml` controls how many runs of history are
kept.

**Layout preservation**: to keep the structure of the original Hive page
(itself Notion-based), each page's HTML is re-fetched at translation time,
and headings, toggles, callouts (with icon and background color),
bulleted/numbered lists, quotes, dividers, tables, column layouts, images,
cover images, and inline links are all extracted as structured blocks.
Storage, review, and Notion rendering all treat a small **Markdown subset**
(documented at the top of
[`monitor/mdblocks.py`](monitor/mdblocks.py)) as the single source of truth.

**Translation reuse**: when a schema change re-extracts pages, previously
translated text is reused from the stored source/translation pairs instead
of being re-sent to DeepL. If the monthly quota runs out mid-page, the
untranslated fragments temporarily keep their source language and the entry
is marked `translation_incomplete`, retried once quota is available again.

**Cross-resource link rewriting**: links between resources (e.g. the
library index linking to its category pages) point at the translated
Notion page once that target exists, falling back to the original Hive page
otherwise. The moment a page is translated for the first time, any
already-synced page linking to it gets relinked in the same run.

**Hierarchy (real Notion sub-pages)**: new pages are created as real
sub-pages mirroring the Hive URL structure, so Notion's sidebar reflects the
original site hierarchy. If the logical parent hasn't been translated yet,
creation is deferred (the Notion API has no way to move a page's parent
after creation).

### Storage format (translations/pages/*.json)

| Field | Contents |
|---|---|
| `url` / `title` / `content_hash` | Info about the source page (a changed hash triggers retranslation) |
| `source_markdown` | The structure-preserving source (Markdown subset) |
| `translated_markdown` | The DeepL translation — the source of truth for Notion rendering |
| `source_text` / `translated_text` | Plain-text copies (for diffing) |
| `schema_version` | Storage format version (a bump triggers retranslation) |
| `review.status` | `unreviewed` → `reviewed` / `fixed` |
| `notion.page_id` etc. | Notion sync bookkeeping |

### Required Secrets

| Secret | Value | Description |
|---|---|---|
| `DEEPL_API_KEY` | A DeepL API key | Free at [DeepL API Free](https://www.deepl.com/pro-api) (500K chars/month). A key ending in `:fx` uses the free-tier endpoint automatically |
| `NOTION_TOKEN` | `ntn_...` | Create at [notion.so/my-integrations](https://www.notion.so/my-integrations) |
| `NOTION_PARENT_PAGE_ID` | A page ID or URL | Where translated pages are created. Share this page with the integration first via "⋯ → Connections" |

If a secret is missing, the corresponding step (translation or Notion sync)
is skipped without erroring.

### Character budget (staying within DeepL's free tier)

Translating everything at once could exceed DeepL's free tier (500K
chars/month), so each run caps how much it translates
(`translation.char_budget_per_run` in `config.yaml`, default 40,000 chars).
The backlog is worked through gradually. Raise this temporarily via the
`char_budget` input on a manual "Run workflow". By default only pages under
`/library` are translated (`translation.include_prefixes`).

### Translation review (Claude routine)

Quality-checking the DeepL output is handled by the bundled Claude Code
skill **`/review-translations`**. Each invocation checks only a handful of
pages (default 3, or e.g. `/review-translations 5`) against their source
and updates `review.status`. Check progress with:

```bash
python monitor/translation_status.py                       # summary
python monitor/translation_status.py --list-unreviewed 5   # next pages to review
```

Fixes made during review are picked up by the next Notion sync.

### Fixing a translation directly in Notion

You don't have to edit the repo — editing text directly on the Notion page
works too. Every `notion_sync.py` run first checks whether the live content
differs from the repo, and if so, pulls the edit back into
`translations/pages/<page>.json` (`review.reviewer` set to
`"notion-manual-edit"`).

Only **content-only edits** (retyping existing block text) are auto-pulled.
Blocks added/removed/reordered by hand, or pages with native sub-page
cards, are skipped — edit `translated_markdown` in the repo directly for
those instead.

## Localization

This pipeline is language-agnostic except for a handful of operator-facing
strings (the updates box, the sync status callout), which live in
[`monitor/locales.py`](monitor/locales.py) as a dict keyed by locale code
(`en` and `ja` included).

To run this for another language:

1. Set `translation.target_lang` in `monitor/config.yaml` to your target
   [DeepL language code](https://developers.deepl.com/docs/getting-started/supported-languages).
2. Set `language` (a locale code) in `monitor/config.yaml`. If it's not
   already in `monitor/locales.py`, copy the `en` block, translate the
   values, and add it under your locale code — everything else follows
   automatically.
3. Nothing else — crawling, DeepL translation, Notion block construction —
   needs to change.

## Caveats

- **JavaScript-rendered sites**: `monitor.py` fetches static HTML only. If
  the captured text is empty or links aren't detected, the target site may
  render via JS — point `config.yaml`'s `sitemap_urls` at a sitemap, or
  switch to a headless browser (Playwright).
- **False positives**: dynamic elements (ads, timestamps, random IDs) can
  register as a diff even when nothing meaningful changed. Exclude the
  affected URLs via `config.yaml`'s `ignore_url_patterns`.

## Layout

```
.
├── .github/workflows/
│   ├── monitor.yml                 # Monitoring: daily at 00:00 UTC (crawl, diff, record)
│   └── translate.yml               # Translation: daily at 01:00 UTC (DeepL → Notion sync)
├── .claude/skills/
│   └── review-translations/        # Claude translation-review routine (/review-translations)
├── monitor/
│   ├── monitor.py                  # Monitor core (crawl, diff, record update history)
│   ├── translate.py                # DeepL translation (saves source + translation as JSON)
│   ├── notion_sync.py              # Creates/updates translated pages, refreshes the updates box
│   ├── translation_status.py       # Shows translation/review progress
│   ├── locales.py                  # Operator-facing message templates, by locale
│   ├── config.yaml                 # Crawl / translation / language configuration
│   └── requirements.txt            # Python dependencies
├── snapshots/
│   ├── state.json                  # The last fetched snapshot (auto-updated by Actions)
│   └── updates.json                # Update history shown in the Notion updates box (auto-updated)
└── translations/
    └── pages/*.json                # Per-page source + translation + review state (auto-updated)
```
