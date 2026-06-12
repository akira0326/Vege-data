# 野菜卸売価格 日次レポート - セットアップ手順

鹿児島市中央卸売市場と宮崎中央青果の野菜卸売価格（中値・円/kg）を毎日取得し、
比較表をメールで自動送信する仕組みです。

対象品目: さつまいも、さといも、ごぼう、キャベツ、にんじん、レタス、はくさい、たまねぎ、じゃがいも

---

## 1. GitHubリポジトリの準備

1. GitHubアカウントを作成（未取得の場合）: https://github.com/
2. 新しいリポジトリを作成（例: `veggie-price-report`）
3. このフォルダ内のファイル一式をリポジトリにアップロード
   - `main.py`
   - `requirements.txt`
   - `scrapers/` フォルダ（`kagoshima_parser.py`, `miyazaki_parser.py`, `__init__.py`）
   - `data/price_history.csv`
   - `.github/workflows/daily_report.yml`

---

## 2. Gmailのアプリパスワード取得

通常のGmailパスワードはセキュリティ上使用できないため、「アプリパスワード」を発行します。

1. Googleアカウントの2段階認証を有効にする
   https://myaccount.google.com/security
2. 「アプリパスワード」を作成
   https://myaccount.google.com/apppasswords
3. 生成された16桁の文字列をメモしておく（後でGitHubのSecretsに登録）

---

## 3. GitHub Secretsの設定

リポジトリの `Settings` → `Secrets and variables` → `Actions` → `New repository secret` で、
以下の3つを登録します。

| Name | 値の例 |
|---|---|
| `GMAIL_ADDRESS` | 送信元のGmailアドレス（例: `your.account@gmail.com`） |
| `GMAIL_APP_PASSWORD` | 手順2で取得した16桁のアプリパスワード |
| `MAIL_TO` | レポートを受け取りたいメールアドレス |

---

## 4. 動作確認（手動実行）

1. リポジトリの `Actions` タブを開く
2. 「Daily Vegetable Price Report」を選択
3. 「Run workflow」ボタンで手動実行
4. 実行ログを確認し、メールが届くか確認

---

## 5. 自動実行スケジュール

`.github/workflows/daily_report.yml` 内の `cron: "0 0 * * *"` で実行時刻を指定しています
（UTC 0:00 = JST 9:00）。市場のPDFが公開される時刻に合わせて調整してください。
例えば JST 12:00 に実行したい場合は `cron: "0 3 * * *"` とします。

---

## 6. 現状の制約・今後の課題

- **宮崎のPDF取得**: `main.py` 内の `fetch_miyazaki_pdf_path()` は、宮崎中央青果の
  ページから本日分のPDFリンクを自動検出する実装になっていますが、サイト構造の変更で
  動かなくなる可能性があります。エラー時は「データなし」として扱われ、処理自体は
  止まりません。
- **市場休場日**: 市場が休みの日（日曜・祝日等）はPDFが存在せず、その日のデータは
  「データなし」になります。
- **先月平均・前年同月平均**: 運用開始直後はデータが蓄積されていないため
  「データ不足」と表示されます。1ヶ月、1年と運用を続けるほど精度が上がります。
- **対象市場の拡張**: 当初検討していた東京・大阪・福岡・名古屋（主要4市場）は、
  無料での自動データ取得が難しいため今回は見送りました。将来的に追加したい場合は
  ご相談ください。

---

## 7. ファイル構成

```
veggie-price-report/
├── .github/workflows/daily_report.yml   # 自動実行設定
├── scrapers/
│   ├── __init__.py
│   ├── kagoshima_parser.py              # 鹿児島市中央卸売市場PDF解析
│   └── miyazaki_parser.py               # 宮崎中央青果PDF解析
├── data/
│   └── price_history.csv                # 価格履歴（自動で蓄積）
├── main.py                              # メイン処理（取得→保存→比較→メール送信）
├── requirements.txt
└── SETUP.md                             # このファイル
```
