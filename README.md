# hive-resources-monitoring

[Hive Resource Library](https://resources.joinhive.org/library) とその配下ページを
定期的に監視し、**更新があればメールで通知**するシステムです。

さらに、取得したページを **DeepL で日本語に自動翻訳**して原文とセットで保存し、
**Notion の個人ページに反映**する翻訳パイプラインを備えています
（→ [翻訳・Notion同期](#翻訳notion同期)）。

## 特徴

- **Claude / LLM のトークンを一切消費しません。** 監視処理は純粋な Python スクリプトで、
  GitHub Actions の定期実行（cron）で動きます。通常運転の AI トークン消費はゼロです。
- 追加の外部サービス不要。GitHub Actions の無料枠内で完結します。
- 前回取得したページ内容のスナップショットをリポジトリ内（`snapshots/state.json`）に保存し、
  次回実行時に差分を比較します。

## 仕組み

```
GitHub Actions (毎日1回 cron)
   └─ monitor/monitor.py
        1. ライブラリページと配下ページをクロールして本文を取得
        2. 各ページの本文ハッシュを計算
        3. snapshots/state.json（前回分）と比較
        4. 追加/削除/変更があれば → メール送信
        5. スナップショットを更新して自動コミット
```

検出する変更:

- **追加されたページ**（新しいリソースが公開された等）
- **削除されたページ**
- **既存ページの内容変更**（本文の差分を unified diff 形式でメールに記載）

## セットアップ

### 1. メール送信用の GitHub Secrets を登録

リポジトリの **Settings → Secrets and variables → Actions → New repository secret** で
以下を登録します（Gmail SMTP を使う構成）。

| Secret 名        | 値の例                          | 説明 |
|------------------|--------------------------------|------|
| `SMTP_USER`      | `youraddress@gmail.com`        | 送信元 Gmail アドレス |
| `SMTP_PASS`      | `abcd efgh ijkl mnop`          | Gmail の**アプリパスワード**（後述） |
| `EMAIL_TO`       | `amanex.watahiki@gmail.com`    | 通知先。カンマ区切りで複数指定可 |
| `EMAIL_FROM`     | `youraddress@gmail.com`        | （任意）送信元。未設定なら `SMTP_USER` を使用 |
| `EMAIL_FROM_NAME`| `Hive Resource Monitor`        | （任意）送信者の表示名 |
| `SMTP_HOST`      | `smtp.gmail.com`               | （任意）未設定なら `smtp.gmail.com` |
| `SMTP_PORT`      | `587`                          | （任意）未設定なら `587` |

#### Gmail アプリパスワードの取得方法

1. Google アカウントで **2 段階認証を有効化**（アプリパスワードには必須）。
2. <https://myaccount.google.com/apppasswords> にアクセス。
3. 任意の名前（例: `hive-monitor`）でアプリパスワードを生成。
4. 表示された 16 桁の文字列を `SMTP_PASS` に登録（スペースはあってもなくても可）。

> 通常の Gmail ログインパスワードは SMTP では使えません。必ずアプリパスワードを使ってください。

Gmail 以外の SMTP サーバや SendGrid などを使いたい場合は、`SMTP_HOST` / `SMTP_PORT` /
`SMTP_USER` / `SMTP_PASS` をそのサービスの値に差し替えてください（SMTP 対応サービスなら
そのまま動きます）。

### 2. Actions の書き込み権限を確認

スナップショットを自動コミットするため、**Settings → Actions → General → Workflow permissions**
で **「Read and write permissions」** を有効にしてください。

### 3. 監視対象・頻度の調整（任意）

- 監視対象・クロール深さ: [`monitor/config.yaml`](monitor/config.yaml)
- チェック頻度: [`.github/workflows/monitor.yml`](.github/workflows/monitor.yml) の `cron` 式
  （デフォルトは毎日 00:00 UTC = 日本時間 09:00）

## 動作確認

初回セットアップ後の流れ:

1. **初回実行**: 差分の比較対象がないため、スナップショットを記録するだけでメールは送りません。
2. **2 回目以降**: 前回との差分を比較し、変更があればメールを送ります。

手動で試すには、Actions 画面で **「Monitor Hive Resource Library」→ Run workflow** を実行します。
`dry_run` に `true` を指定すると、メールを送らずに検出した差分をログに出力できます
（Secrets 登録前の動作確認に便利）。

ローカルで試す場合:

```bash
pip install -r monitor/requirements.txt
DRY_RUN=true python monitor/monitor.py
```

## 翻訳・Notion同期

`monitor.py` が取得したページを DeepL API で日本語に翻訳し、原文・訳文をセットで
リポジトリに保存、さらに Notion の個人ページに反映します。**翻訳・同期とも純粋な
Python スクリプトで、Claude のトークンは消費しません。**

```
GitHub Actions (毎日 01:00 UTC = JST 10:00)
   └─ translate.py   : snapshots/state.json の新規・変更ページを DeepL で翻訳
   │                    → translations/pages/<ページ>.json に原文+訳文を保存
   └─ notion_sync.py : 未同期の訳文を Notion 親ページ配下に 1ページ=1リソースで作成/更新
```

### 保存形式（translations/pages/*.json）

1ページ 1 JSON ファイル。フィールド:

| フィールド | 内容 |
|---|---|
| `url` / `title` / `content_hash` | 元ページの情報（ハッシュが変わると再翻訳） |
| `source_text` | クロールした原文テキスト |
| `translated_text` | DeepL による日本語訳（レビューで修正可） |
| `review.status` | `unreviewed` → `reviewed` / `fixed`（Claudeレビューで更新） |
| `notion.page_id` ほか | Notion 同期の管理情報 |

### 必要な Secrets（追加分）

| Secret 名 | 値 | 説明 |
|---|---|---|
| `DEEPL_API_KEY` | DeepL API キー | [DeepL API Free](https://www.deepl.com/pro-api) で無料登録（50万字/月）。キー末尾が `:fx` なら Free 用エンドポイントを自動選択 |
| `NOTION_TOKEN` | `ntn_...` | [notion.so/my-integrations](https://www.notion.so/my-integrations) で内部インテグレーションを作成して取得 |
| `NOTION_PARENT_PAGE_ID` | ページID または ページURL | 訳文ページの作成先となる親ページ。**そのページの「⋯ → コネクト」でインテグレーションを追加**（共有）しておくこと |

`DEEPL_API_KEY` 未設定なら翻訳はスキップ、`NOTION_TOKEN` 未設定なら Notion 同期は
スキップされます（エラーにはなりません）。

### 文字数バジェット（DeepL 無料枠対策）

全ページを一度に翻訳すると DeepL 無料枠（50万字/月）を超え得るため、1回の実行で
翻訳する原文文字数に上限を設けています（`config.yaml` の
`translation.char_budget_per_run`、デフォルト 40,000字）。未翻訳分は日次実行で
少しずつ消化されます。手動実行（Run workflow）の `char_budget` 入力で一時的に
引き上げることもできます。翻訳対象はデフォルトで `/library` 配下のみです
（`translation.include_prefixes` で変更可）。

### 翻訳レビュー（Claude ルーチン）

DeepL 訳の品質チェックは、リポジトリ同梱の Claude Code スキル
**`/review-translations`** で行います。1回の呼び出しで数ページのみ
（デフォルト3、`/review-translations 5` のように指定可）を原文と対照チェックし、
`review.status` を更新してコミットします。少しずつ実行することでトークン消費を
抑えられます。進捗確認は:

```bash
python monitor/translation_status.py                  # サマリ
python monitor/translation_status.py --list-unreviewed 5   # 次のレビュー対象
```

レビューで訳文を修正すると、次回の Notion 同期で修正後の内容がページに反映されます。

## 注意事項

- **JavaScript で描画されるサイトの場合**: `monitor.py` は静的 HTML を取得します。もし取得した
  ページ本文が空だったり、リンクが検出できない場合は、対象サイトが JS レンダリングの可能性が
  あります。その際は `config.yaml` の `sitemap_urls` にサイトマップを指定するか、ヘッドレス
  ブラウザ（Playwright）への切り替えが必要です。まずは初回実行のログで取得ページ数を確認して
  ください。
- **誤検知（false positive）**: 広告・日付表示・ランダムIDなど動的に変わる要素があると、実質的な
  更新がなくても差分として検出されることがあります。その場合は `config.yaml` の
  `ignore_url_patterns` で対象URLを除外するか、必要に応じて本文抽出ロジックを調整してください。

## ファイル構成

```
.
├── .github/workflows/
│   ├── monitor.yml                 # 監視: 毎日 00:00 UTC（クロール・差分・メール）
│   └── translate.yml               # 翻訳: 毎日 01:00 UTC（DeepL → Notion 同期）
├── .claude/skills/
│   └── review-translations/        # Claude 翻訳レビュー・ルーチン（/review-translations）
├── monitor/
│   ├── monitor.py                  # 監視本体（クロール・差分・メール送信）
│   ├── translate.py                # DeepL 翻訳（原文+訳文を JSON 保存）
│   ├── notion_sync.py              # 訳文を Notion 親ページ配下に作成/更新
│   ├── translation_status.py       # 翻訳・レビュー進捗の表示
│   ├── config.yaml                 # 監視対象・クロール・翻訳設定
│   └── requirements.txt            # Python 依存パッケージ
├── snapshots/
│   └── state.json                  # 前回取得したスナップショット（Actions が自動更新）
└── translations/
    └── pages/*.json                # ページ毎の原文+日本語訳+レビュー状態（Actions が自動更新）
```
