# hive-resources-monitoring

Periodically monitors the [Hive Resource Library](https://resources.joinhive.org/library)
and the pages under it, and **emails you when something is added, removed, or
changed**.

It also includes an optional translation pipeline: fetched pages can be
**machine-translated via DeepL** and saved alongside the source text, then
**synced to a personal Notion page** (→ [Translation & Notion sync](#translation--notion-sync)).

## Features

- **Uses zero Claude / LLM tokens.** The monitoring itself is plain Python,
  run on a schedule (cron) by GitHub Actions. Normal operation consumes no
  AI tokens at all.
- No extra external services required — runs entirely within GitHub Actions'
  free tier.
- The previous run's page snapshot is stored in the repo itself
  (`snapshots/state.json`) and diffed against on the next run.

## How it works

```
GitHub Actions (cron, once a day)
   └─ monitor/monitor.py
        1. Crawl the library and its sub-pages, extracting page text
        2. Hash each page's content
        3. Compare against snapshots/state.json (the previous run)
        4. If anything was added / removed / changed → send an email
        5. Update the snapshot and commit it automatically
```

Changes it detects:

- **Pages added** (e.g. a new resource published)
- **Pages removed**
- **Existing pages changed** (the email includes a unified diff of the body text)

## Setup

### 1. Register GitHub Secrets for sending email

Under the repo's **Settings → Secrets and variables → Actions → New
repository secret**, add the following (this is set up for Gmail SMTP):

| Secret               | Example value                   | Description |
|-----------------------|----------------------------------|--------------|
| `SMTP_USER`           | `youraddress@gmail.com`         | The sending Gmail address |
| `SMTP_PASS`           | `abcd efgh ijkl mnop`           | A Gmail **app password** (see below) |
| `EMAIL_TO`            | `you@example.com`               | Where notifications go. Comma-separate for multiple recipients |
| `EMAIL_FROM`          | `youraddress@gmail.com`         | (optional) Sender address; defaults to `SMTP_USER` |
| `EMAIL_FROM_NAME`     | `Hive Resource Monitor`         | (optional) Sender display name |
| `SMTP_HOST`           | `smtp.gmail.com`                | (optional) Defaults to `smtp.gmail.com` |
| `SMTP_PORT`           | `587`                           | (optional) Defaults to `587` |

#### Getting a Gmail app password

1. **Enable 2-Step Verification** on your Google account (required for app
   passwords).
2. Go to <https://myaccount.google.com/apppasswords>.
3. Generate an app password under any name (e.g. `hive-monitor`).
4. Copy the 16-character string into `SMTP_PASS` (spaces are fine either
   way).

> Your normal Gmail login password won't work over SMTP — you must use an
> app password.

To use a different SMTP provider (another mailbox, SendGrid, etc.), just
point `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` at that
service's values — anything that speaks SMTP works as-is.

### 2. Confirm Actions has write permission

The workflow commits the updated snapshot back to the repo, so under
**Settings → Actions → General → Workflow permissions**, enable
**"Read and write permissions"**.

### 3. Adjust what's monitored and how often (optional)

- Crawl targets / depth: [`monitor/config.yaml`](monitor/config.yaml)
- Check frequency: the `cron` expression in
  [`.github/workflows/monitor.yml`](.github/workflows/monitor.yml)
  (defaults to daily at 00:00 UTC)

## Verifying it works

After initial setup:

1. **First run**: there's nothing to diff against yet, so it just records a
   snapshot — no email is sent.
2. **From the second run on**: it diffs against the previous snapshot and
   emails you if anything changed.

To try it manually, go to the Actions tab and run **"Monitor Hive Resource
Library" → Run workflow**. Setting `dry_run` to `true` logs the detected
diff instead of emailing it (handy for testing before Secrets are set up).

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
                       lists, quotes, dividers, and links as Notion blocks)
```

**Layout preservation**: to keep the structure of the original Hive page (it's
itself Notion-based), each page's HTML is re-fetched at translation time and
its headings, bulleted/numbered lists, quotes, dividers, and inline links are
extracted as structured blocks. Translation goes through DeepL's HTML tag
handling, so links and bold text survive. Storage, review, and Notion
rendering all treat a small **Markdown subset**
(`#`/`##`/`###`, `- `/`1. `, `> `, `---`, `[text](URL)`, `**bold**`) as the
single source of truth.

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
operator-facing strings (monitor emails, the Notion sync status callout, the
"not yet translated" link annotation). Those live in one place —
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
   construction — needs no changes.

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
│   ├── monitor.yml                 # Monitoring: daily at 00:00 UTC (crawl, diff, email)
│   └── translate.yml               # Translation: daily at 01:00 UTC (DeepL → Notion sync)
├── .claude/skills/
│   └── review-translations/        # Claude translation-review routine (/review-translations)
├── monitor/
│   ├── monitor.py                  # Monitor core (crawl, diff, send email)
│   ├── translate.py                # DeepL translation (saves source + translation as JSON)
│   ├── notion_sync.py              # Creates/updates translated pages under the Notion parent
│   ├── translation_status.py       # Shows translation/review progress
│   ├── locales.py                  # Operator-facing message templates, by locale
│   ├── config.yaml                 # Crawl / translation / language configuration
│   └── requirements.txt            # Python dependencies
├── snapshots/
│   └── state.json                  # The last fetched snapshot (auto-updated by Actions)
└── translations/
    └── pages/*.json                # Per-page source + translation + review state (auto-updated by Actions)
```
