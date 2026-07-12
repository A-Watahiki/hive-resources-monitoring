# hive-resources-monitoring

Periodically monitors the [Hive Resource Library](https://resources.joinhive.org/library)
and the pages under it, and **shows what was added, removed, or changed in an
"updates" box on your Notion top page**. There is no email involved, so no
SMTP credentials or Gmail app-password setup are needed — which also makes
spinning up another-language fork one step simpler.

It also includes the translation pipeline that feeds that Notion page:
fetched pages are **machine-translated via DeepL** and saved alongside the
source text, then **synced to a personal Notion page** (→ [Translation & Notion sync](#translation--notion-sync)).

## Features

- **Uses zero Claude / LLM tokens.** The monitoring itself is plain Python,
  run on a schedule (cron) by GitHub Actions. Normal operation consumes no
  AI tokens at all.
- No extra external services required — runs entirely within GitHub Actions'
  free tier. No email/SMTP configuration at all.
- The previous run's page snapshot is stored in the repo itself
  (`snapshots/state.json`) and diffed against on the next run; detected
  changes accumulate as a history in `snapshots/updates.json`.

## How it works

```
GitHub Actions (cron, once a day)
   └─ monitor/monitor.py
        1. Crawl the library and its sub-pages, extracting page text
        2. Hash each page's content
        3. Compare against snapshots/state.json (the previous run)
        4. If anything was added / removed / changed → record it in
           snapshots/updates.json (the update history)
        5. Update the snapshot and commit it automatically
   └─ an hour later, notion_sync.py renders that update history as a
      📢 "updates" callout box on the Notion top page (see below)
```

Changes it detects:

- **Pages added** (e.g. a new resource published)
- **Pages removed**
- **Existing pages changed** (the update entry keeps a unified diff of the
  body text, shown in a collapsed "show diff" toggle)

## Setup

### 1. Confirm Actions has write permission

The workflow commits the updated snapshot back to the repo, so under
**Settings → Actions → General → Workflow permissions**, enable
**"Read and write permissions"**.

### 2. Adjust what's monitored and how often (optional)

- Crawl targets / depth: [`monitor/config.yaml`](monitor/config.yaml)
- Check frequency: the `cron` expression in
  [`.github/workflows/monitor.yml`](.github/workflows/monitor.yml)
  (defaults to daily at 00:00 UTC)

## Verifying it works

After initial setup:

1. **First run**: there's nothing to diff against yet, so it just records a
   snapshot — no update entry is recorded.
2. **From the second run on**: it diffs against the previous snapshot and,
   if anything changed, appends an entry to `snapshots/updates.json`; the
   next Notion sync (the translate workflow, an hour later) shows it in the
   updates box on the top page.

To try it manually, go to the Actions tab and run **"Monitor Hive Resource
Library" → Run workflow**. Setting `dry_run` to `true` logs the detected
diff without recording it or updating the snapshot.

To try it locally:

```bash
pip install -r monitor/requirements.txt
DRY_RUN=true python monitor/monitor.py
```

## Translation & Notion sync

`translate.py` translates the pages `monitor.py` fetched via the DeepL API,
saves the source and translation side by side in the repo, and syncs them to
a personal Notion page. **Both the translation and sync steps are plain
Python — they don't use any Claude tokens either.**

```
GitHub Actions (daily at 01:00 UTC)
   └─ translate.py  : re-fetches new/changed pages' HTML, extracting
   │                  structure (headings, lists, links, etc.) rather than
   │                  flat text → translates via DeepL
   │                  → saves source + translation to
   │                    translations/pages/<page>.json
   └─ notion_sync.py: creates/updates one Notion page per untranslated
                       resource under a parent page (rendering headings,
                       lists, quotes, dividers, and links as Notion blocks),
                       and refreshes the 📢 updates box on that parent page
```

**The updates box**: `notion_sync.py` renders the update history recorded by
the monitor (`snapshots/updates.json`) as a 📢 callout box on the top
(parent) page. Each monitoring run that found changes becomes one collapsed
toggle (`2026-07-12: 3 change(s)`) listing the added / removed / changed
pages; titles link to the translated Notion page when one exists (falling
back to the original page), and changed pages carry their text diff in a
nested "show diff" toggle. The box is created automatically on first sync —
you can drag it anywhere on the page afterwards; later runs only refresh its
contents (it's found again by its 📢 icon). `updates_keep` in
[`monitor/config.yaml`](monitor/config.yaml) controls how many runs of
history are kept.

**Layout preservation**: to keep the structure of the original Hive page (it's
itself Notion-based), each page's HTML is re-fetched at translation time and
its headings, toggles (plain and heading toggles), callouts (with icon and
background color), bulleted/numbered lists, quotes, dividers, tables, column
layouts, images, page covers, and inline links are extracted as structured
blocks. Translation goes through DeepL's HTML tag handling, so links and bold
text survive. Storage, review, and Notion rendering all treat a small
**Markdown subset** (documented at the top of
[`monitor/mdblocks.py`](monitor/mdblocks.py): `#`/`##`/`###`, `#>`/`>>>`
toggles, `!!!(icon|color)` callouts, `- `/`1. `, `> `, `---`, `| a | b |`
tables, `|||`/`||` columns, `![alt](url)` images, `[text](URL)`, `**bold**`)
as the single source of truth.

**Translation reuse**: when a schema change re-extracts pages, previously
translated text is reused from the stored source/translation pairs instead of
being re-sent to DeepL, so layout upgrades cost little or no quota. If the
monthly DeepL quota runs out mid-page, the untranslated fragments temporarily
keep their source language, the entry is marked `translation_incomplete`, and
the page is retried on later runs until the quota allows it to finish.

**Cross-resource link rewriting**: links between resources (e.g. the library
index linking to its category pages) are automatically rewritten to point at
the corresponding **translated Notion page** once that target has been
translated. Links to not-yet-translated pages fall back to the original
(English) Hive page. The moment a page is translated for the first time, any
already-synced page that links to it gets its rendering refreshed
("relinked") in the same run, so the link is upgraded without needing a
retranslation of the linking page.

**Hierarchy (real Notion sub-pages)**: newly created Notion pages are created
as **real sub-pages** of the page matching their logical parent in the Hive
URL structure (e.g. `/library/ai-prompts/academia` under the already-created
`/library/ai-prompts` page). This means Notion's sidebar and child-page lists
mirror the original Hive hierarchy. If the logical parent hasn't been
translated yet, page creation is deferred until it has (the Notion API has no
way to move a page's parent after creation, so this is decided once, at
creation time).

### Storage format (translations/pages/*.json)

One JSON file per page. Fields:

| Field | Contents |
|---|---|
| `url` / `title` / `content_hash` | Info about the source page (a changed hash triggers retranslation) |
| `source_markdown` | The structure-preserving source (Markdown subset) |
| `translated_markdown` | The DeepL translation (Markdown subset — the source of truth for Notion rendering; edited during review) |
| `source_text` / `translated_text` | Plain-text copies (for diffing / progress checks) |
| `schema_version` | Storage format version (a bump triggers retranslation) |
| `review.status` | `unreviewed` → `reviewed` / `fixed` (updated by the Claude review routine) |
| `notion.page_id` etc. | Notion sync bookkeeping |

### Required Secrets (additional)

| Secret | Value | Description |
|---|---|---|
| `DEEPL_API_KEY` | A DeepL API key | Free registration at [DeepL API Free](https://www.deepl.com/pro-api) (500K chars/month). A key ending in `:fx` automatically selects the free-tier endpoint |
| `NOTION_TOKEN` | `ntn_...` | Create an internal integration at [notion.so/my-integrations](https://www.notion.so/my-integrations) |
| `NOTION_PARENT_PAGE_ID` | A page ID or page URL | The parent page new translated pages are created under. **Share that page with the integration** via "⋯ → Connections" first |

If `DEEPL_API_KEY` isn't set, translation is skipped; if `NOTION_TOKEN` isn't
set, the Notion sync is skipped — neither is treated as an error.

### Character budget (staying within DeepL's free tier)

Translating everything in one go could exceed DeepL's free tier (500K
chars/month), so each run caps how much source text it translates
(`translation.char_budget_per_run` in `config.yaml`, default 40,000 chars).
The backlog is worked through gradually across daily runs. You can raise this
temporarily via the `char_budget` input on a manual "Run workflow". By
default only pages under `/library` are translated (change this with
`translation.include_prefixes`).

### Translation review (Claude routine)

Quality-checking the DeepL output is handled by the bundled Claude Code
skill **`/review-translations`**. Each invocation checks only a handful of
pages against their source (default 3, or e.g. `/review-translations 5`),
updates `review.status`, and commits. Running it a little at a time keeps
token usage low. Check progress with:

```bash
python monitor/translation_status.py                       # summary
python monitor/translation_status.py --list-unreviewed 5   # next pages to review
```

Fixes made during review are picked up by the next Notion sync.

### Fixing a translation directly in Notion

You don't have to edit the repo to fix a mistranslation — editing the text
directly on the Notion page works too. Before pushing anything, every
`notion_sync.py` run first checks each already-synced page for edits: if
the live Notion content differs from what the repo has, the edit is pulled
back into that page's `translations/pages/<page>.json`
(`translated_markdown` / `translated_text`), and its `review` status is set
to `fixed` (`reviewer: "notion-manual-edit"`) — all committed and pushed to
GitHub automatically. This keeps the repo authoritative, so the next sync
won't overwrite your fix with the old text.

This only auto-pulls **content-only edits** — retyping/correcting the text
of an existing block. It's skipped (left for you to fix in
`translated_markdown` directly instead) when:
- Blocks were added, deleted, or reordered by hand (the live structure no
  longer matches what this pipeline would render), or
- The page has any native sub-page cards (the `link_to_page` blocks used
  for the "linked resources" lists — e.g. a library index page) — these
  can't be round-tripped back into Markdown, so pages containing them are
  skipped entirely, even for edits elsewhere on the same page.

If in doubt, editing `translated_markdown` in the repo is always the more
reliable path — the Notion edit path is a convenience for quick fixes.

## Localization

Everything in this pipeline is language-agnostic except a handful of
operator-facing strings (the updates box on the Notion top page, the sync
status callout, the "not yet translated" link annotation). Those live in one
place —
[`monitor/locales.py`](monitor/locales.py) — as a dict keyed by locale code,
with `en` and `ja` (this deployment's live example) built in.

To run this for another target language:

1. Set `translation.target_lang` in `monitor/config.yaml` to your target
   DeepL language code (see
   [DeepL's supported languages](https://developers.deepl.com/docs/getting-started/supported-languages)).
2. Set the top-level `language` key in `monitor/config.yaml` to a locale
   code. If it's not already in `monitor/locales.py`, copy the `en` block
   there, translate the values, and add it under your locale code —
   everything else in the pipeline picks it up automatically.
3. Everything else — crawling, DeepL translation, Notion block
   construction — needs no changes. (There is no email step, so no
   SMTP/Gmail credentials to obtain or register.)

## Caveats

- **JavaScript-rendered sites**: `monitor.py` fetches static HTML. If the
  page text it captures is empty, or links aren't detected, the target site
  may render via JS. In that case, either point `config.yaml`'s
  `sitemap_urls` at a sitemap, or switch to a headless browser (Playwright).
  Check the first run's logs for the page count fetched to spot this early.
- **False positives**: dynamic elements (ads, timestamps, random IDs) can
  register as a diff even when nothing meaningful changed. If that happens,
  exclude the affected URLs via `config.yaml`'s `ignore_url_patterns`, or
  adjust the body-extraction logic as needed.

## Layout

```
.
├── .github/workflows/
│   ├── monitor.yml                 # Monitoring: daily at 00:00 UTC (crawl, diff, record updates)
│   └── translate.yml               # Translation: daily at 01:00 UTC (DeepL → Notion sync)
├── .claude/skills/
│   └── review-translations/        # Claude translation-review routine (/review-translations)
├── monitor/
│   ├── monitor.py                  # Monitor core (crawl, diff, record update history)
│   ├── translate.py                # DeepL translation (saves source + translation as JSON)
│   ├── notion_sync.py              # Creates/updates translated pages under the Notion parent
│   ├── translation_status.py       # Shows translation/review progress
│   ├── locales.py                  # Operator-facing message templates, by locale
│   ├── config.yaml                 # Crawl / translation / language configuration
│   └── requirements.txt            # Python dependencies
├── snapshots/
│   ├── state.json                  # The last fetched snapshot (auto-updated by Actions)
│   └── updates.json                # Update history shown in the Notion updates box (auto-updated)
└── translations/
    └── pages/*.json                # Per-page source + translation + review state (auto-updated by Actions)
```
