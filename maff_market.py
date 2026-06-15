"""
野菜卸売価格 日次レポート - メインスクリプト（全国版・両方併用）

データ源:
 1. 全国主要市場 … 農水省「青果物市況情報」（maff_market.py 経由・無料）
    関東・関西・中国・九州の主要市場＋鹿児島市場を、産地別に取得
 2. 宮崎市場 … 既存スクレイパー（scrapers/miyazaki_parser.py）を補助で併用
    ※宮崎は全国ソースに無いため

処理フロー:
 1. 上記2系統から本日（無ければ直近）の産地別 中値/高値/安値/入荷量 を取得
 2. data/price_history.csv（産地別スキーマ）に追記
 3. 履歴から「前回比」「先月平均」「前年同月平均」を計算
 4. HTMLレポート作成（A:エリア別サマリー / B:注目市場 鹿児島・宮崎 / C:品目別 産地TOP）
 5. メール送信（Gmail SMTP）

必要な環境変数:
 GMAIL_ADDRESS      : 送信元Gmailアドレス
 GMAIL_APP_PASSWORD : Gmailアプリパスワード
 MAIL_TO            : 送信先（カンマ区切り可）
"""

import os
import csv
import smtplib
from datetime import date, datetime
from collections import defaultdict
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import maff_market

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
HISTORY_CSV = os.path.join(DATA_DIR, "price_history.csv")
LEGACY_CSV = os.path.join(DATA_DIR, "price_history_legacy.csv")

HISTORY_HEADER = ["date", "market", "area", "item", "origin", "volume_t", "high", "mid", "low"]

ITEMS = ["ごぼう", "さつまいも", "さといも"]
AREAS = ["関東", "関西", "中国", "九州"]
# 注目市場（コード or 名称で対応）。鹿児島=API(46300)、宮崎=スクレイパー。
FEATURED = [("46300", "鹿児島"), ("宮崎", "宮崎")]


# ---------------------------------------------------------------------------
# 取得
# ---------------------------------------------------------------------------
def to_iso(date_str: str) -> str:
    """'2026/06/15' -> '2026-06-15'（不正なら空文字）"""
    date_str = (date_str or "").strip().replace("-", "/")
    try:
        return datetime.strptime(date_str, "%Y/%m/%d").date().isoformat()
    except ValueError:
        return ""


def fetch_miyazaki(today: date):
    """
    宮崎市場を既存スクレイパーで取得し、全国データと同じレコード形式へ変換。
    scrapers/miyazaki_parser.py が無い／取得失敗時は空リストを返す（全国分のみで継続）。
    """
    try:
        from scrapers.miyazaki_parser import extract_prices as extract_miyazaki
    except Exception as e:
        print(f"[情報] 宮崎スクレイパー未使用: {e}")
        return []

    # 既存の宮崎PDF取得ロジック（直近7日遡及）は従来の main.py から流用してください。
    # ここでは取得関数 fetch_miyazaki_pdf_path(target)->path を想定。
    try:
        from datetime import timedelta
        from miyazaki_fetch import fetch_miyazaki_pdf_path  # 既存の取得関数を切り出した想定
    except Exception:
        fetch_miyazaki_pdf_path = None

    records = []
    if fetch_miyazaki_pdf_path is None:
        print("[情報] 宮崎PDF取得関数が見つからないためスキップ")
        return records

    from datetime import timedelta
    for offset in range(0, 7):
        target = today - timedelta(days=offset)
        try:
            pdf_path = fetch_miyazaki_pdf_path(target)
        except Exception as e:
            print(f"[警告] 宮崎リンク取得失敗 {target}: {e}")
            continue
        if not pdf_path:
            continue
        try:
            prices = extract_miyazaki(pdf_path)
        except Exception as e:
            print(f"[警告] 宮崎PDF解析失敗 {target}: {e}")
            continue
        for item in ITEMS:
            d = prices.get(item)
            if not d:
                continue
            records.append({
                "date": target.strftime("%Y/%m/%d"),
                "market": "宮崎", "market_code": "宮崎", "area": "九州",
                "item": item, "origin": "宮崎",
                "volume_t": None,
                "high": None, "mid": d.get("中値_円per_kg"), "low": None,
            })
        if records:
            break
    return records


def fetch_today_records(today: date):
    """全国（maff_market）＋宮崎（scraper）を取得して結合"""
    records = []
    try:
        records.extend(maff_market.fetch_all())
    except Exception as e:
        print(f"[警告] 全国市況の取得失敗: {e}")
    records.extend(fetch_miyazaki(today))
    return records


# ---------------------------------------------------------------------------
# 履歴
# ---------------------------------------------------------------------------
def migrate_history_if_needed():
    """旧スキーマ(price_per_kg列)のCSVがあれば legacy へ退避し、新スキーマで開始する"""
    if not os.path.exists(HISTORY_CSV):
        return
    with open(HISTORY_CSV, encoding="utf-8") as f:
        header = f.readline().strip().split(",")
    if header != HISTORY_HEADER:
        os.replace(HISTORY_CSV, LEGACY_CSV)
        print(f"[情報] 旧スキーマの履歴を {LEGACY_CSV} へ退避しました")


def append_to_history(records):
    """(date,market,item,origin) 重複を避けつつ追記"""
    os.makedirs(DATA_DIR, exist_ok=True)
    existing = set()
    exists = os.path.exists(HISTORY_CSV)
    if exists:
        with open(HISTORY_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing.add((row["date"], row["market"], row["item"], row["origin"]))

    with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(HISTORY_HEADER)
        for r in records:
            iso = to_iso(r["date"])
            if not iso:
                continue
            key = (iso, r["market"], r["item"], r["origin"])
            if key in existing:
                continue
            existing.add(key)
            w.writerow([iso, r["market"], r.get("area", ""), r["item"], r["origin"],
                        r.get("volume_t") if r.get("volume_t") is not None else "",
                        r.get("high") if r.get("high") is not None else "",
                        r.get("mid") if r.get("mid") is not None else "",
                        r.get("low") if r.get("low") is not None else ""])


def load_history():
    if not os.path.exists(HISTORY_CSV):
        return []
    rows = []
    with open(HISTORY_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            for k in ("volume_t", "high", "mid", "low"):
                row[k] = float(row[k]) if row[k] not in ("", None) else None
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 集計（産地行 -> 代表中値）
# ---------------------------------------------------------------------------
def _agg_mid(rows):
    """産地別行の中値を入荷量で加重平均（数量不明なら単純平均）。値が無ければNone"""
    vals = [(r["mid"], r["volume_t"]) for r in rows if r["mid"] is not None]
    if not vals:
        return None
    wsum = sum(m * v for m, v in vals if v)
    vsum = sum(v for _, v in vals if v)
    if vsum > 0:
        return round(wsum / vsum)
    return round(sum(m for m, _ in vals) / len(vals))


def index_history(history):
    """(date,scope,item)->代表中値 を market別・area別で作る"""
    by_market = defaultdict(list)
    by_area = defaultdict(list)
    for r in history:
        by_market[(r["date"], r["market"], r["item"])].append(r)
        if r.get("area"):
            by_area[(r["date"], r["area"], r["item"])].append(r)
    mp = {k: _agg_mid(v) for k, v in by_market.items()}
    ap = {k: _agg_mid(v) for k, v in by_area.items()}
    return mp, ap


def latest_date_for(index, scope, item, on_or_before=None):
    """index((date,scope,item)) の中で、最新（指定日以前）で値のある日付を返す"""
    cand = [d for (d, s, it) in index if s == scope and it == item
            and index[(d, s, it)] is not None
            and (on_or_before is None or d <= on_or_before)]
    return max(cand) if cand else None


def prev_value(index, scope, item, before):
    cand = [d for (d, s, it) in index if s == scope and it == item
            and index[(d, s, it)] is not None and d < before]
    if not cand:
        return None
    return index[(max(cand), scope, item)]


def month_avg(history, scope_key, scope_val, item, year, month):
    """指定年月の代表中値の平均（市場 or エリア指定）"""
    daily = defaultdict(list)
    for r in history:
        if r[scope_key] != scope_val or r["item"] != item:
            continue
        d = r["date"]
        if int(d[:4]) == year and int(d[5:7]) == month:
            daily[d].append(r)
    mids = [m for m in (_agg_mid(v) for v in daily.values()) if m is not None]
    if not mids:
        return None
    return round(sum(mids) / len(mids))


def origin_month_avg_vol(history, origin, item, year, month):
    """指定年月の、その産地×品目の平均日次入荷量(t/日)。データが無ければNone"""
    daily = defaultdict(float)
    for r in history:
        if r["origin"] != origin or r["item"] != item or r["volume_t"] is None:
            continue
        d = r["date"]
        if int(d[:4]) == year and int(d[5:7]) == month:
            daily[d] += r["volume_t"]
    if not daily:
        return None
    return round(sum(daily.values()) / len(daily), 1)


# ---------------------------------------------------------------------------
# レポート
# ---------------------------------------------------------------------------
def _fmt(v, suffix="円"):
    return f"{v}{suffix}" if v is not None else "データ不足"


def build_report(today: date, history) -> str:
    iso = today.isoformat()
    mp, ap = index_history(history)

    if today.month == 1:
        lm_y, lm = today.year - 1, 12
    else:
        lm_y, lm = today.year, today.month - 1
    ly_y, ly_m = today.year - 1, today.month

    # --- A: エリア別サマリー（中値・前回比） ---
    a_rows = ""
    for item in ITEMS:
        cells = ""
        for area in AREAS:
            d = latest_date_for(ap, area, item, on_or_before=iso)
            if d is None:
                cells += "<td colspan='3'>データなし</td>"
                continue
            cur = ap[(d, area, item)]
            prev = prev_value(ap, area, item, d)
            diff = f"{cur - prev:+d}円" if (prev is not None and cur is not None) else "データ不足"
            lya = month_avg(history, "area", area, item, ly_y, ly_m)
            tag = "" if d == iso else f"<br><span style='color:#999;font-size:11px'>{d[5:].replace('-', '/')}時点</span>"
            cells += f"<td>{_fmt(cur)}{tag}</td><td>{diff}</td><td>{_fmt(lya)}</td>"
        a_rows += f"<tr><td><b>{item}</b></td>{cells}</tr>\n"
    area_header = "<tr><th>品目</th>" + "".join(
        f"<th colspan='3'>{a}</th>" for a in AREAS) + "</tr><tr><th></th>" + \
        "<th>中値</th><th>前回比</th><th>前年同月平均</th>" * len(AREAS) + "</tr>"

    # --- B: 注目市場（鹿児島・宮崎）×品目 フル指標 ---
    b_rows = ""
    for item in ITEMS:
        cells = ""
        for code, label in FEATURED:
            scope = label  # 履歴のmarket名で照合（鹿児島/宮崎）
            d = latest_date_for(mp, scope, item, on_or_before=iso)
            if d is None:
                cells += "<td colspan='4'>データなし</td>"
                continue
            cur = mp[(d, scope, item)]
            prev = prev_value(mp, scope, item, d)
            diff = f"{cur - prev:+d}円" if (prev is not None and cur is not None) else "データ不足"
            lma = month_avg(history, "market", scope, item, lm_y, lm)
            lya = month_avg(history, "market", scope, item, ly_y, ly_m)
            cells += f"<td>{_fmt(cur)}</td><td>{diff}</td><td>{_fmt(lma)}</td><td>{_fmt(lya)}</td>"
        b_rows += f"<tr><td><b>{item}</b></td>{cells}</tr>\n"
    feat_header = "<tr><th>品目</th>" + "".join(
        f"<th colspan='4'>{lbl}</th>" for _, lbl in FEATURED) + "</tr><tr><th></th>" + \
        "<th>価格</th><th>前回比</th><th>先月平均</th><th>前年同月平均</th>" * len(FEATURED) + "</tr>"

    # --- C: 主要産地別（品目ごとの 中値・当日入荷量・前年同月入荷量） ---
    latest_iso = max((r["date"] for r in history), default=iso)
    cur = defaultdict(lambda: {"vol": 0.0, "midw": 0.0, "midv": 0.0})
    origin_total = defaultdict(float)
    for r in history:
        if r["date"] != latest_iso or r["mid"] is None:
            continue
        v = r["volume_t"] or 0
        a = cur[(r["origin"], r["item"])]
        a["vol"] += v
        a["midw"] += r["mid"] * v
        a["midv"] += v
        origin_total[r["origin"]] += v
    top_origins = [o for o, _ in sorted(origin_total.items(),
                   key=lambda kv: kv[1], reverse=True)[:8]]
    c_blocks = "" if top_origins else "<p>データなし</p>"
    for origin in top_origins:
        items_here = [it for it in ITEMS if (origin, it) in cur]
        if not items_here:
            continue
        items_here.sort(key=lambda x: cur[(origin, x)]["vol"], reverse=True)
        lines = ""
        for it in items_here:
            a = cur[(origin, it)]
            mid = round(a["midw"] / a["midv"]) if a["midv"] > 0 else None
            lyv = origin_month_avg_vol(history, origin, it, ly_y, ly_m)
            ly_str = f"{lyv} t/日" if lyv is not None else "データ不足"
            lines += (f"<tr><td>{it}</td><td>{_fmt(mid)}</td>"
                      f"<td>{a['vol']:.1f} t</td><td>{ly_str}</td></tr>")
        c_blocks += (f"<p style='margin:10px 0 2px'><b>{origin}</b>（{latest_iso} 全国合算）</p>"
                     f"<table border='1' cellpadding='4' cellspacing='0'>"
                     f"<tr><th>品目</th><th>中値</th><th>当日入荷量</th>"
                     f"<th>前年同月入荷量</th></tr>{lines}</table>")

    return f"""<html><body>
<h2>【野菜卸売価格レポート】{today.strftime('%Y年%m月%d日')}（中値・実勢価格ベース）</h2>

<h3>A. エリア別サマリー</h3>
<table border="1" cellpadding="5" cellspacing="0">{area_header}{a_rows}</table>

<h3>B. 注目市場（鹿児島・宮崎）</h3>
<table border="1" cellpadding="5" cellspacing="0">{feat_header}{b_rows}</table>

<h3>C. 主要産地別</h3>
{c_blocks}

<p style="font-size:12px;color:#666;">
※「データ不足」は、運用開始から十分な履歴が蓄積されていないことを示します。<br>
※「データなし」は、直近で該当データが見つからなかったことを示します。<br>
※「(MM/DD時点)」は本日分が未公開のため直近の確定データを使用していることを示します。<br>
※「前年同月入荷量」は、前年同月の平均日次入荷量(t/日)です。<br>
※ 価格は円/kg・税込。全国市場は農水省「青果物市況情報」、宮崎は宮崎中央青果より。
</p>
</body></html>"""


# ---------------------------------------------------------------------------
# メール
# ---------------------------------------------------------------------------
def _send(to: str, subject: str, body: str, is_html: bool):
    """低レベル送信。to はカンマ区切り可。"""
    addr = os.environ["GMAIL_ADDRESS"]
    pw = os.environ["GMAIL_APP_PASSWORD"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = addr
    msg["To"] = to
    msg.attach(MIMEText(body, "html" if is_html else "plain", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(addr, pw)
        s.send_message(msg)


def send_email(subject: str, html_body: str):
    """通常レポートを宛先(MAIL_TO)へ送信"""
    _send(os.environ["MAIL_TO"], subject, html_body, is_html=True)


def notify_admin(subject: str, body: str):
    """
    運用者(送信元アドレス自身)へ警告/失敗を通知する。
    通知自体の失敗で本処理を巻き込まないよう、例外は握りつぶす。
    """
    try:
        to = os.environ.get("GMAIL_ADDRESS", "")
        if to:
            _send(to, f"[野菜レポート監視] {subject}", body, is_html=False)
    except Exception as e:
        print(f"[警告] 管理者通知の送信に失敗: {e}")


def main():
    today = date.today()
    print(f"=== {today.isoformat()} 全国版レポート処理開始 ===")
    try:
        migrate_history_if_needed()
        records = fetch_today_records(today)
        print(f"取得レコード数: {len(records)}")

        # 取得ゼロの監視: 開市日(平日)に1件も取れない＝取得元の仕様変更を疑う
        usable = [r for r in records if r.get("mid") is not None]
        if not usable and today.weekday() < 5:
            notify_admin(
                "本日のデータ取得がゼロでした",
                f"{today.isoformat()} の実行で、対象品目の有効データが1件も取得できませんでした。\n"
                "取得元（青果物市況情報 / 宮崎）の仕様変更や障害の可能性があります。\n"
                "Actionsのログと取得元サイトをご確認ください。",
            )

        append_to_history(records)
        history = load_history()
        html = build_report(today, history)
        send_email(f"【野菜卸売価格レポート】{today.strftime('%Y/%m/%d')}", html)
        print("=== 処理完了 ===")
    except Exception:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        notify_admin(
            "レポート処理が失敗しました",
            f"{today.isoformat()} の実行で例外が発生しました。\n\n{tb}",
        )
        raise  # Actions側も失敗(赤)として残す


if __name__ == "__main__":
    main()
