# hive-resources-monitoring

[Hive Resource Library](https://resources.joinhive.org/library) とその配下ページを
定期的に監視し、**更新があればメールで通知**するシステムです。

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
├── .github/workflows/monitor.yml   # cron スケジュール & 実行ワークフロー
├── monitor/
│   ├── monitor.py                  # 監視本体（クロール・差分・メール送信）
│   ├── config.yaml                 # 監視対象・クロール設定
│   └── requirements.txt            # Python 依存パッケージ
└── snapshots/
    └── state.json                  # 前回取得したスナップショット（Actions が自動更新）
```
