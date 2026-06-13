bash

cat /mnt/user-data/outputs/veggie-price-report/main.py
出力

"""
野菜卸売価格 日次レポート - メインスクリプト

処理フロー:
  1. 鹿児島・宮崎の本日PDFを取得し、9品目の中値(円/kg)を抽出
  2. data/price_history.csv に本日分を追記
  3. 履歴CSVから「前日比」「先月平均」「前年同月平均」を計算
  4. 比較表(HTML)を作成
  5. メール送信(Gmail SMTP)

実行方法:
  python3 main.py
  （GitHub Actionsから毎日定時実行する想定）

必要な環境変数:
  GMAIL_ADDRESS    : 送信元Gmailアドレス
  GMAIL_APP_PASSWORD : Gmailアプリパスワード
  MAIL_TO          : 送信先メールアドレス
"""

import os
import csv
import smtplib
from datetime import date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from scrapers.kagoshima_parser import fetch_pdf as fetch_kagoshima_pdf, extract_prices as extract_kagoshima
from scrapers.miyazaki_parser import extract_prices as extract_miyazaki

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
HISTORY_CSV = os.path.join(DATA_DIR, "price_history.csv")

ITEMS = [
    "さつまいも", "さといも", "ごぼう", "キャベツ", "にんじん",
    "レタス", "はくさい", "たまねぎ", "じゃがいも",
]
MARKETS = ["鹿児島", "宮崎"]


def fetch_today_prices(today: date):
    """
    最新の確定済み価格データを取得する。
    本日分が未公開の場合（土日や、まだ卸売結果が出ていない当日）は、
    直近7日間を遡って最初に見つかったデータを使用する。

    戻り値:
      results: { "鹿児島": {item: price_per_kg or None, ...}, "宮崎": {...} }
      data_dates: { "鹿児島": date or None, "宮崎": date or None }  # 実際に取得できたデータの日付
    """
    results = {"鹿児島": {}, "宮崎": {}}
    data_dates = {"鹿児島": None, "宮崎": None}

    # --- 鹿児島: 本日から最大7日遡って探す ---
    for offset in range(0, 7):
        target = today - timedelta(days=offset)
        date_str = target.strftime("%Y%m%d")
        try:
            pdf_bytes = fetch_kagoshima_pdf(date_str)
        except Exception:
            # YYYYMMDD形式で無ければ、月初の "MDD" 形式も試す（鹿児島市サイトの一部命名の揺れに対応）
            try:
                alt_str = f"{target.month}{target.day:02d}"
                pdf_bytes = fetch_kagoshima_pdf(alt_str)
            except Exception:
                continue

        try:
            kagoshima_prices = extract_kagoshima(pdf_bytes)
            for item in ITEMS:
                data = kagoshima_prices.get(item)
                results["鹿児島"][item] = data["中値_円per_kg"] if data else None
            data_dates["鹿児島"] = target
            break
        except Exception as e:
            print(f"[警告] 鹿児島PDF解析失敗 ({date_str}): {e}")
            continue
    else:
        print("[警告] 鹿児島データ取得失敗: 直近7日間でPDFが見つかりませんでした")
        for item in ITEMS:
            results["鹿児島"][item] = None

    # --- 宮崎: 本日から最大7日遡って探す ---
    for offset in range(0, 7):
        target = today - timedelta(days=offset)
        try:
            pdf_path = fetch_miyazaki_pdf_path(target)
        except Exception as e:
            print(f"[警告] 宮崎PDFリンク取得失敗 ({target.isoformat()}): {e}")
            continue

        if not pdf_path:
            continue

        try:
            miyazaki_prices = extract_miyazaki(pdf_path)
            for item in ITEMS:
                data = miyazaki_prices.get(item)
                results["宮崎"][item] = data["中値_円per_kg"] if data else None
            data_dates["宮崎"] = target
            break
        except Exception as e:
            print(f"[警告] 宮崎PDF解析失敗 ({target.isoformat()}): {e}")
            continue
    else:
        print("[警告] 宮崎データ取得失敗: 直近7日間でPDFが見つかりませんでした")
        for item in ITEMS:
            results["宮崎"][item] = None

    return results, data_dates


def fetch_miyazaki_pdf_path(today: date):
    """
    宮崎中央青果のページから本日分の「青果物市況野菜」PDFのURLを取得し、
    一時ファイルにダウンロードしてそのパスを返す。

    TODO: 実装が必要。
    https://www.miyaseiren.com/shikyo_data/category/chuou/ をスクレイピングし、
    本日の日付(YYYYMMDD)を含むリンクテキストから「青果物市況野菜-N.pdf」の
    URLを特定してダウンロードする。
    """
    import re
    import requests

    list_url = "https://www.miyaseiren.com/shikyo_data/category/chuou/"
    resp = requests.get(list_url, timeout=30)
    resp.raise_for_status()
    html = resp.text

    date_str = today.strftime("%Y%m%d")
    # 「宮崎中央青果 YYYYMMDD野菜」というタイトルの直後にあるPDFリンクを探す
    pattern = rf"{date_str}野菜.*?href=\"(https://[^\"]+?\.pdf)\""
    m = re.search(pattern, html, re.DOTALL)
    if not m:
        return None

    pdf_url = m.group(1)
    pdf_resp = requests.get(pdf_url, timeout=30)
    pdf_resp.raise_for_status()

    tmp_path = f"/tmp/miyazaki_{today.strftime('%Y%m%d')}.pdf"
    with open(tmp_path, "wb") as f:
        f.write(pdf_resp.content)
    return tmp_path


def append_to_history(prices: dict, data_dates: dict):
    """各市場のデータ日付ごとに price_history.csv へ追記する（既存の同一日・同一市場・同一品目の行は重複させない）"""
    existing = set()
    if os.path.exists(HISTORY_CSV):
        with open(HISTORY_CSV, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing.add((row["date"], row["market"], row["item"]))

    file_exists = os.path.exists(HISTORY_CSV)
    with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["date", "market", "item", "price_per_kg"])
        for market in MARKETS:
            d = data_dates.get(market)
            if d is None:
                continue
            for item in ITEMS:
                key = (d.isoformat(), market, item)
                if key in existing:
                    continue
                price = prices[market].get(item)
                writer.writerow([d.isoformat(), market, item, price if price is not None else ""])


def load_history() -> list:
    """price_history.csv を読み込み、辞書のリストとして返す"""
    if not os.path.exists(HISTORY_CSV):
        return []
    rows = []
    with open(HISTORY_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["price_per_kg"]:
                row["price_per_kg"] = float(row["price_per_kg"])
            else:
                row["price_per_kg"] = None
            rows.append(row)
    return rows


def get_price_on(history: list, target_date: date, market: str, item: str):
    """指定日の価格を取得（なければNone）"""
    ds = target_date.isoformat()
    for row in history:
        if row["date"] == ds and row["market"] == market and row["item"] == item:
            return row["price_per_kg"]
    return None


def get_month_average(history: list, year: int, month: int, market: str, item: str):
    """指定の年月の平均価格を計算（データが無ければNone）"""
    vals = []
    for row in history:
        d = row["date"]
        y, m = int(d[:4]), int(d[5:7])
        if y == year and m == month and row["market"] == market and row["item"] == item:
            if row["price_per_kg"] is not None:
                vals.append(row["price_per_kg"])
    if not vals:
        return None
    return round(sum(vals) / len(vals), 1)


def build_report(today: date, today_prices: dict, data_dates: dict, history: list) -> str:
    """HTML形式の比較レポートを作成する"""

    rows_html = ""
    for item in ITEMS:
        cells = []
        for market in MARKETS:
            d = data_dates.get(market)
            price_today = today_prices[market].get(item)

            if d is None or price_today is None:
                cells.append("<td colspan='4'>データなし</td>")
                continue

            # 「前日」= このデータ日付より前の、履歴上で最も新しい記録
            prev_price = None
            prev_date = None
            for row in sorted(history, key=lambda r: r["date"], reverse=True):
                if row["market"] == market and row["item"] == item and row["date"] < d.isoformat():
                    if row["price_per_kg"] is not None:
                        prev_price = row["price_per_kg"]
                        prev_date = row["date"]
                    break

            if today.month == 1:
                last_month_year, last_month = today.year - 1, 12
            else:
                last_month_year, last_month = today.year, today.month - 1
            last_year_year, last_year_month = today.year - 1, today.month

            last_month_avg = get_month_average(history, last_month_year, last_month, market, item)
            last_year_avg = get_month_average(history, last_year_year, last_year_month, market, item)

            if prev_price is not None:
                diff = price_today - prev_price
                diff_str = f"{diff:+.1f}円"
            else:
                diff_str = "データ不足"

            last_month_str = f"{last_month_avg}円" if last_month_avg is not None else "データ不足"
            last_year_str = f"{last_year_avg}円" if last_year_avg is not None else "データ不足"

            cells.append(
                f"<td>{price_today}円</td>"
                f"<td>{diff_str}</td>"
                f"<td>{last_month_str}</td>"
                f"<td>{last_year_str}</td>"
            )

        rows_html += f"<tr><td rowspan='1'><b>{item}</b></td>{''.join(cells)}</tr>\n"

    def market_label(market):
        d = data_dates.get(market)
        if d is None:
            return f"{market}（データなし）"
        if d == today:
            return market
        return f"{market}（{d.strftime('%m/%d')}時点）"

    header = (
        "<tr>"
        "<th>品目</th>"
        f"<th colspan='4'>{market_label('鹿児島')}</th>"
        f"<th colspan='4'>{market_label('宮崎')}</th>"
        "</tr>"
        "<tr><th></th>"
        + "<th>価格</th><th>前回比</th><th>先月平均</th><th>前年同月平均</th>" * 2
        + "</tr>"
    )

    html = f"""
    <html><body>
    <h2>【野菜卸売価格レポート】{today.strftime('%Y年%m月%d日')}（中値・実勢価格ベース）</h2>
    <table border="1" cellpadding="5" cellspacing="0">
        {header}
        {rows_html}
    </table>
    <p style="font-size:12px;color:#666;">
    ※「データ不足」は、運用開始から十分な期間のデータが蓄積されていないことを示します。<br>
    ※「データなし」は、該当市場の直近7日間にデータが見つからなかったことを示します。<br>
    ※ 市場名の後の「(MM/DD時点)」は、本日分のデータが未公開のため、直近の確定データを使用していることを示します。
    </p>
    </body></html>
    """
    return html


def send_email(subject: str, html_body: str):
    """Gmail SMTPでメールを送信する"""
    gmail_address = os.environ["GMAIL_ADDRESS"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]
    mail_to = os.environ["MAIL_TO"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = mail_to
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_address, gmail_app_password)
        server.send_message(msg)


def main():
    today = date.today()

    print(f"=== {today.isoformat()} 野菜価格レポート処理開始 ===")

    today_prices, data_dates = fetch_today_prices(today)
    print("取得した価格:", today_prices)
    print("データ日付:", data_dates)

    append_to_history(today_prices, data_dates)
    history = load_history()

    html = build_report(today, today_prices, data_dates, history)

    subject = f"【野菜卸売価格レポート】{today.strftime('%Y/%m/%d')}"
    send_email(subject, html)

    print("=== 処理完了 ===")


if __name__ == "__main__":
    main()
完了

上記の内容を、GitHub上のmain.py編集画面で全選択→削除→貼り付けしてください。

貼り付けたら、下にスクロールして「Commit changes」をクリックしてください。その後、もう一度「Actions」タブから手動実行(Run workflow)して、結果を確認しましょう。


