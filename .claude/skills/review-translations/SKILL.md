---
name: review-translations
description: DeepL翻訳された Hive Resource Library ページの訳文を原文と対照してチェックし、必要なら修正して review ステータスを更新する。1回の実行で少数ページのみ処理し、トークン消費を抑えつつ徐々にレビューを進める。引数でページ数を指定可能（例 /review-translations 5、デフォルト3）。
---

# 翻訳レビュー・ルーチン

`translations/pages/*.json` に保存された DeepL 翻訳を、原文と対照してチェックする。
**1回の呼び出しで処理するのは少数ページのみ**（デフォルト3、引数があればその数）。
残りは次回以降の呼び出しで徐々に進める。

## 手順

1. レビュー対象を取得する:

   ```bash
   python monitor/translation_status.py --list-unreviewed <N>
   ```

   出力される各行は `ファイルパス、URL、原文文字数`。未レビューが無ければ
   その旨をユーザーに報告して終了する。

2. 各ファイルを Read で読み、`source_markdown`（原文）と `translated_markdown`
   （DeepL訳）を対照して以下の観点でチェックする:
   - **誤訳・意味の反転**（否定の取り違え、主語の混同など）
   - **訳抜け**（原文にあって訳文にない段落・文）
   - **用語**: 動物福祉/アドボカシー分野の定訳（例: animal welfare → 動物福祉、
     advocacy → アドボカシー、factory farming → 工場式畜産）。団体名・人名・
     プログラム名は無理に訳さず原語のままでよい
   - **ナビゲーション由来のノイズ**が不自然に訳されていても、本文が正しければ許容

3. 修正が必要な場合のみ Edit で `translated_markdown` を直す。全文の書き直しは
   しない — 問題箇所のピンポイント修正に留める。**Markdownの記法は保つこと**:
   見出しの `#`／`##`／`###`、箇条書きの `- `・`1. `、引用の `> `、区切りの
   `---`、リンクの `[表示文字](URL)`、強調の `**太字**` は壊さない（Notionの
   レイアウト描画に使われる）。リンクのURLは変更しない。
   `translated_text`（プレーンテキスト版）も同様に直しておくとよいが、Notionへの
   反映は `translated_markdown` が使われる。

4. 各ファイルの `review` オブジェクトを更新する:
   - 修正なし → `"status": "reviewed"`
   - 修正あり → `"status": "fixed"`
   - 共通: `"reviewed_at"` に現在時刻（UTC, ISO 8601）、`"reviewer": "claude"`、
     `"notes"` に一行サマリ（日本語、例: "誤訳2箇所を修正" / "問題なし"）

   **注意**: `notion.synced_hash` を `null` にすると次回のNotion同期で
   修正後の訳文がNotionページに反映される。`translated_text` を修正した
   場合は必ず `null` にする。修正なしの場合はそのままでよい。

5. まとめてコミット・プッシュする:

   ```bash
   git add translations/ && git commit -m "review: translation check (N pages)" && git push
   ```

6. ユーザーに報告する: 何ページ確認し、何箇所修正したか、残りの未レビュー数
   （`python monitor/translation_status.py` の出力から）。
