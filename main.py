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


def fetch_today_prices(today: date) -> dict:
    """
    本日分の価格を取得する。
    戻り値: { "鹿児島": {item: price_per_kg or None, ...}, "宮崎": {...} }

    NOTE: 宮崎のPDF取得URLは現状「ページをスクレイピングしてその日のリンクを
    見つける」処理が必要。ここではURL取得部分は別関数(fetch_miyazaki_pdf_path)
    に委ね、未実装の場合はNoneを返す。
    """
    results = {"鹿児島": {}, "宮崎": {}}

    # --- 鹿児島 ---
    date_str = today.strftime("%Y%m%d")
    try:
        pdf_bytes = fetch_kagoshima_pdf(date_str)
        kagoshima_prices = extract_kagoshima(pdf_bytes)
        for item in ITEMS:
            data = kagoshima_prices.get(item)
            results["鹿児島"][item] = data["中値_円per_kg"] if data else None
    except Exception as e:
        print(f"[警告] 鹿児島データ取得失敗: {e}")
        for item in ITEMS:
            results["鹿児島"][item] = None

    # --- 宮崎 ---
    try:
        pdf_path = fetch_miyazaki_pdf_path(today)
        if pdf_path:
            miyazaki_prices = extract_miyazaki(pdf_path)
            for item in ITEMS:
                data = miyazaki_prices.get(item)
                results["宮崎"][item] = data["中値_円per_kg"] if data else None
        else:
            raise RuntimeError("本日分PDFのURLが取得できませんでした")
    except Exception as e:
        print(f"[警告] 宮崎データ取得失敗: {e}")
        for item in ITEMS:
            results["宮崎"][item] = None

    return results


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

    tmp_path = "/tmp/miyazaki_today.pdf"
    with open(tmp_path, "wb") as f:
        f.write(pdf_resp.content)
    return tmp_path


def append_to_history(today: date, prices: dict):
    """本日分を price_history.csv に追記する"""
    file_exists = os.path.exists(HISTORY_CSV)
    with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["date", "market", "item", "price_per_kg"])
        for market in MARKETS:
            for item in ITEMS:
                price = prices[market].get(item)
                writer.writerow([today.isoformat(), market, item, price if price is not None else ""])


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


def build_report(today: date, today_prices: dict, history: list) -> str:
    """HTML形式の比較レポートを作成する"""
    yesterday = today - timedelta(days=1)

    # 先月・前年同月の年月を計算
    if today.month == 1:
        last_month_year, last_month = today.year - 1, 12
    else:
        last_month_year, last_month = today.year, today.month - 1
    last_year_year, last_year_month = today.year - 1, today.month

    rows_html = ""
    for item in ITEMS:
        cells = []
        for market in MARKETS:
            price_today = today_prices[market].get(item)
            price_yesterday = get_price_on(history, yesterday, market, item)
            last_month_avg = get_month_average(history, last_month_year, last_month, market, item)
            last_year_avg = get_month_average(history, last_year_year, last_year_month, market, item)

            if price_today is None:
                cells.append("<td colspan='4'>データなし</td>")
                continue

            if price_yesterday is not None:
                diff = price_today - price_yesterday
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

    header = (
        "<tr>"
        "<th>品目</th>"
        "<th colspan='4'>鹿児島</th>"
        "<th colspan='4'>宮崎</th>"
        "</tr>"
        "<tr><th></th>"
        + "<th>本日</th><th>前日比</th><th>先月平均</th><th>前年同月平均</th>" * 2
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
    ※「データなし」は、当日その品目の取引が無かったことを示します。
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

    today_prices = fetch_today_prices(today)
    print("本日の価格:", today_prices)

    append_to_history(today, today_prices)
    history = load_history()

    html = build_report(today, today_prices, history)

    subject = f"【野菜卸売価格レポート】{today.strftime('%Y/%m/%d')}"
    send_email(subject, html)

    print("=== 処理完了 ===")


if __name__ == "__main__":
    main()
