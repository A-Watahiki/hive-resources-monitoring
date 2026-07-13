# hive-resources-monitoring

[Hive Resource Library](https://resources.joinhive.org/library) を定期監視し、
更新内容を Notion 上の「更新情報」ボックスに表示します。DeepL による自動翻訳を
経て、翻訳版ページとして個人の Notion に同期する機能も含みます。すべて
GitHub Actions 上で動く Python スクリプトで完結し、AIトークンは消費しません。

## 🔰 プログラミング未経験でもできるクイックスタート

**ターミナルやコマンドライン、プログラミングの知識は一切不要です。** すべて
ブラウザ上の操作だけで、あなたの言語版の翻訳ページが作れます（所要時間の目安：
15分＋自動処理待ち）。

1. **このリポジトリをフォークする**
   ページ右上の **Fork** ボタンを押し、自分のGitHubアカウントにコピーを作成します。
2. **DeepL APIキーを取得する**（無料）
   [DeepL API Free](https://www.deepl.com/pro-api) に登録し、APIキーをコピーして
   おきます（`:fx` で終わるキーが無料枠です）。
3. **Notion側を準備する**
   - [notion.so/my-integrations](https://www.notion.so/my-integrations) で
     「New integration」を作成し、表示された **トークン**（`ntn_...`）をコピー。
   - 翻訳ページの置き場所にしたい Notion ページを1つ用意し、右上の **⋯ →
     Connections** から、今作ったインテグレーションを追加（共有）します。
   - そのページを開いた状態のブラウザURLをコピーしておきます（ページIDとして使います）。
4. **フォークしたリポジトリにキーを登録する**
   自分のリポジトリの **Settings → Secrets and variables → Actions → New
   repository secret** から、以下の3つを1つずつ登録します。

   | Secret名 | 値 |
   |---|---|
   | `DEEPL_API_KEY` | 手順2でコピーしたDeepLのAPIキー |
   | `NOTION_TOKEN` | 手順3でコピーしたNotionのトークン |
   | `NOTION_PARENT_PAGE_ID` | 手順3でコピーしたNotionページのURL |
5. **Actionsに書き込み権限を与える**
   **Settings → Actions → General → Workflow permissions** で
   **「Read and write permissions」** を選び、保存します。
6. **翻訳先の言語を設定する**
   リポジトリ内の `monitor/config.yaml` をブラウザ上で開き、鉛筆アイコン（Edit
   this file）で直接編集します。
   - `language:` を自分の言語のロケールコード（例: `"ko"`、`"fr"`）に変更
   - `translation.target_lang:` を [DeepLの対応言語コード](https://developers.deepl.com/docs/getting-started/supported-languages)
     （例: `"KO"`、`"FR"`）に変更
   - 画面下部の「Commit changes」で保存

   > `en` と `ja` 以外の言語を選んだ場合は、追加でもう1ファイル
   > （`monitor/locales.py`）に短い定型文の翻訳を数行足す必要があります。
   > 詳しくは末尾の「[多言語対応](#多言語対応)」を参照してください（AIアシスタント
   > に頼んでやってもらうのも手軽です）。
7. **手動で一度動かして確認する**
   **Actions** タブを開き、まず **「Monitor Hive Resource Library」→ Run
   workflow** を実行。数分後に完了したら、続けて **「Translate & Sync to
   Notion」→ Run workflow** を実行します（初回は数十分かかることがあります）。
8. **Notionを確認する**
   手順3で用意したNotionページを開き、翻訳されたページが増えていくのを確認します。
   以降は自動的に毎日実行されます（クロール: 毎日00:00 UTC、翻訳: 01:00
   UTC。変更したい場合は `.github/workflows/*.yml` の `cron` を編集）。

これで完了です。ここから先は、より詳しい仕組みや細かいカスタマイズ方法の
リファレンスです。

## 使い方の概要

```
GitHub Actions（毎日 00:00 UTC）
   └─ monitor.py    : ライブラリをクロールし、前回スナップショットと比較。
                       変更があれば snapshots/updates.json に記録
GitHub Actions（毎日 01:00 UTC、1時間後）
   └─ translate.py   : 新規・変更ページをDeepLで翻訳し、
                        translations/pages/*.json に保存
   └─ notion_sync.py : 翻訳をNotionページへ反映し、
                        📢 更新情報ボックスも更新
```

検出される変更：ページの**追加**・**削除**・**内容変更**（変更ページは本文の
差分を保持し、更新情報ボックスの「差分を表示」トグルで確認できます）。

## Features

- **AIトークンを消費しません。** 監視・翻訳・Notion同期はすべて素のPython。
- GitHub Actionsの無料枠内で完結（メール送信なし、外部サービス不要）。
- 直前のスナップショットは `snapshots/state.json` にリポジトリ内で保持され、
  次回実行時の差分検出に使われます。

## ローカルで試す（任意）

```bash
pip install -r monitor/requirements.txt
DRY_RUN=true python monitor/monitor.py
```

`DRY_RUN=true` は検出した差分をログ出力するだけで、スナップショットも更新
情報も書き換えません。

## Translation & Notion sync の詳細

**更新情報ボックス**：`notion_sync.py` が `snapshots/updates.json` の履歴を
Notionの親ページ上に 📢 コールアウトとして描画します。変更があった実行ごとに
1つの折りたたみトグル（例：`2026-07-12：3件の変更`）にまとまり、タイトルは
翻訳済みNotionページ（未翻訳なら元ページ）にリンクします。ボックスは初回同期時
に自動作成され、ページ内の好きな位置に移動しても構いません（📢アイコンで再度
発見されます）。保持する実行回数は `config.yaml` の `updates_keep` で調整できます。

**レイアウト保持**：元のHiveページ（Notionベース）の構造を保つため、翻訳時に
HTMLを再取得し、見出し・トグル・コールアウト（アイコン・背景色付き）・
箇条書き/番号付きリスト・引用・区切り線・テーブル・カラムレイアウト・画像・
カバー画像・インラインリンクを構造化ブロックとして抽出します。保存・レビュー・
Notion描画はすべて小さな **Markdownサブセット**
（[`monitor/mdblocks.py`](monitor/mdblocks.py) 冒頭に仕様あり）を単一の
ソース・オブ・トゥルースとして扱います。

**翻訳の再利用**：スキーマ変更で再抽出が走っても、既存の翻訳ペアから
テキストを再利用し、DeepLへの再送信を避けます。月間クォータを使い切った
場合は、未翻訳の断片だけ元言語のまま残り `translation_incomplete` としてマーク
され、クォータが回復し次第、後続の実行で再試行されます。

**サイト内リンクの自動付け替え**：リソース間のリンク（例：ライブラリ索引から
各カテゴリページへのリンク）は、リンク先が翻訳済みなら翻訳版Notionページへ、
未翻訳ならHiveの原文ページへ自動的に向けられます。あるページが初めて翻訳
された瞬間、それにリンクしている既存の同期済みページも同じ実行内で
再描画（リンクの付け替え）されます。

**階層構造（実際のNotionサブページ）**：新規作成ページは、Hive URLの階層に
対応する実際のNotionサブページとして作成されます（サイドバーが元サイトの
構造を反映）。論理上の親がまだ翻訳されていない場合、作成は後の実行まで
保留されます（Notion APIには作成後に親を変更する方法がないため）。

### ストレージ形式（translations/pages/*.json）

| フィールド | 内容 |
|---|---|
| `url` / `title` / `content_hash` | 原文ページの情報（ハッシュ変更で再翻訳） |
| `source_markdown` | 構造を保持した原文（Markdownサブセット） |
| `translated_markdown` | DeepL翻訳結果（Notion描画のソース・オブ・トゥルース） |
| `source_text` / `translated_text` | プレーンテキスト版（差分確認用） |
| `schema_version` | ストレージ形式のバージョン（変更で再翻訳） |
| `review.status` | `unreviewed` → `reviewed` / `fixed` |
| `notion.page_id` 等 | Notion同期の管理情報 |

### 必要なSecrets

| Secret | 値 | 説明 |
|---|---|---|
| `DEEPL_API_KEY` | DeepLのAPIキー | [DeepL API Free](https://www.deepl.com/pro-api)（月50万文字まで無料）。`:fx` 終わりのキーは自動的に無料枠エンドポイントを使用 |
| `NOTION_TOKEN` | `ntn_...` | [notion.so/my-integrations](https://www.notion.so/my-integrations) で作成 |
| `NOTION_PARENT_PAGE_ID` | ページIDまたはURL | 翻訳ページの作成先。事前に「⋯ → Connections」でインテグレーションと共有すること |

未設定の場合、該当ステップ（翻訳またはNotion同期）はエラーにせずスキップされます。

### DeepL文字数の予算管理

無制限に翻訳するとDeepLの無料枠（月50万文字）を超える可能性があるため、
1回の実行あたりの翻訳量は `config.yaml` の `translation.char_budget_per_run`
（既定40,000文字）で制限されています。残りは日々の実行で少しずつ処理されます。
手動実行時は `char_budget` 入力で一時的に引き上げ可能です。翻訳対象は既定で
`/library` 配下のみです（`translation.include_prefixes` で変更）。

### 翻訳レビュー（Claudeルーチン）

DeepL訳の品質チェックは付属のClaude Codeスキル **`/review-translations`**
が担当します。1回の実行につき数ページのみ（既定3、`/review-translations 5`
のように指定可）を原文と照合し、`review.status` を更新してコミットします。
進捗確認：

```bash
python monitor/translation_status.py                       # サマリー
python monitor/translation_status.py --list-unreviewed 5   # 次にレビューすべきページ
```

レビューでの修正は次回のNotion同期で反映されます。

### Notion上で直接翻訳を修正する

リポジトリを編集しなくても、Notionページ上で直接テキストを直せます。
`notion_sync.py` は毎回、同期済みページの内容が生きているNotionと食い違って
いないか先にチェックし、食い違いがあれば修正内容を `translations/pages/<page>.json`
に取り込んでコミット・プッシュします（`review.reviewer` は
`"notion-manual-edit"` に）。

自動的に取り込まれるのは**内容のみの編集**（既存ブロックのテキストの書き換え）
です。ブロックの追加・削除・並べ替えがあった場合や、ページ内にネイティブの
サブページカードがある場合はスキップされるため、その際は
`translated_markdown` をリポジトリ側で直接編集してください。

## 多言語対応

このパイプラインはほぼ言語非依存ですが、更新情報ボックスや同期ステータス
コールアウトなど、一部の運用者向け文言だけは
[`monitor/locales.py`](monitor/locales.py) にロケールコードごとの辞書として
まとまっています（`en` と `ja` を同梱）。

別の言語で動かす手順：

1. `monitor/config.yaml` の `translation.target_lang` を目的の
   [DeepL言語コード](https://developers.deepl.com/docs/getting-started/supported-languages)
   に設定。
2. `monitor/config.yaml` の `language`（ロケールコード）を設定。
   `monitor/locales.py` に未登録なら `en` ブロックをコピーして翻訳し、
   同じロケールコードで追加すれば、他の部分は自動で追随します。
3. クロール・DeepL翻訳・Notionブロック構築など、それ以外の変更は不要です。

## 既知の制約

- **JavaScriptで描画されるサイト**：`monitor.py` は静的HTMLのみ取得します。
  取得テキストが空、またはリンクが検出されない場合、対象サイトがJS描画の
  可能性があります。その場合は `config.yaml` の `sitemap_urls` にサイトマップ
  を指定するか、ヘッドレスブラウザ（Playwright）への切り替えを検討してください。
- **誤検知**：広告・タイムスタンプ・ランダムIDなど動的要素が、実質的な変更が
  なくても差分として検出されることがあります。該当URLは `config.yaml` の
  `ignore_url_patterns` で除外してください。

## ディレクトリ構成

```
.
├── .github/workflows/
│   ├── monitor.yml                 # 監視：毎日00:00 UTC（クロール・差分検出・記録）
│   └── translate.yml               # 翻訳：毎日01:00 UTC（DeepL → Notion同期）
├── .claude/skills/
│   └── review-translations/        # Claude翻訳レビュールーチン（/review-translations）
├── monitor/
│   ├── monitor.py                  # 監視の本体（クロール・差分・更新履歴の記録）
│   ├── translate.py                # DeepL翻訳（原文＋訳文をJSONで保存）
│   ├── notion_sync.py              # 翻訳ページの作成・更新、更新情報ボックスの反映
│   ├── translation_status.py       # 翻訳・レビューの進捗表示
│   ├── locales.py                  # ロケールごとの運用者向け文言
│   ├── config.yaml                 # クロール／翻訳／言語の設定
│   └── requirements.txt            # Python依存パッケージ
├── snapshots/
│   ├── state.json                  # 直前のスナップショット（Actionsが自動更新）
│   └── updates.json                # 更新情報ボックスに表示する履歴（自動更新）
└── translations/
    └── pages/*.json                # ページごとの原文・訳文・レビュー状態（自動更新）
```
