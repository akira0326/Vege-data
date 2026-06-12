"""
鹿児島市中央卸売市場（青果市場）の卸売結果PDFから、
指定品目の中値を円/kgに換算して抽出するスクリプト。

PDF URL パターン:
  https://www.city.kagoshima.lg.jp/keizai/chuouoroshi/seika/documents/kekka-YYYYMMDD.pdf

PDFの行構成（実物確認済み）:
  [品目, 品目計(t), 産地, 入荷量(t), 高値(円), 中値(円), 安値(円), 量目(kg), 等級, 階級]
  ※ 品目計が空欄の行は、直前行と同じ品目の別産地/別規格

使い方:
  python3 kagoshima_parser.py 20260612
"""

import sys
import re
import requests
import pdfplumber
import io

BASE_URL = "https://www.city.kagoshima.lg.jp/keizai/chuouoroshi/seika/documents/kekka-{date}.pdf"

# 対象品目: PDF表記 -> 表示名
TARGET_ITEMS = {
    "かんしょ": "さつまいも",
    "さといも": "さといも",
    "ごぼう": "ごぼう",
    "キャベツ": "キャベツ",
    "にんじん": "にんじん",
    "レタス": "レタス",
    "はくさい": "はくさい",
    "たまねぎ": "たまねぎ",
    "ばれいしょ": "じゃがいも",
}


def fetch_pdf(date_str: str) -> bytes:
    url = BASE_URL.format(date=date_str)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content


def to_number(s):
    """'1,404' -> 1404.0 / '-' や空欄 -> None"""
    if s is None:
        return None
    s = str(s).strip().replace(",", "")
    if s in ("", "-", "ー", "−"):
        return None
    s = re.sub(r"[^\d.]", "", s)
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def extract_prices(pdf_bytes: bytes) -> dict:
    """
    各対象品目について、入荷量が最大の行を代表値として
    中値(円/kg換算)を計算する。
    """
    candidates = {v: [] for v in TARGET_ITEMS.values()}

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                last_item = None
                for row in table:
                    if not row or len(row) < 8:
                        continue
                    item_raw = (row[0] or "").strip()

                    matched_item = None
                    if item_raw in TARGET_ITEMS:
                        matched_item = TARGET_ITEMS[item_raw]
                        last_item = matched_item
                    elif item_raw == "" and last_item is not None:
                        matched_item = last_item
                    else:
                        last_item = None
                        continue

                    arrival = to_number(row[3]) or 0.0
                    takane = to_number(row[4])
                    chuune = to_number(row[5])
                    yasune = to_number(row[6])
                    ryome = to_number(row[7])
                    sanchi = (row[2] or "").strip()

                    if ryome is None or ryome == 0:
                        continue

                    if chuune is None:
                        vals = [v for v in (takane, yasune) if v is not None]
                        if not vals:
                            continue
                        chuune = sum(vals) / len(vals)

                    candidates[matched_item].append((arrival, chuune, ryome, sanchi))

    results = {}
    for item, rows in candidates.items():
        if not rows:
            results[item] = None
            continue
        with_arrival = [r for r in rows if r[0] > 0]
        best = max(with_arrival or rows, key=lambda r: r[0])
        arrival, chuune, ryome, sanchi = best
        price_per_kg = chuune / ryome
        results[item] = {
            "中値_円": chuune,
            "量目_kg": ryome,
            "中値_円per_kg": round(price_per_kg, 1),
            "産地": sanchi,
            "入荷量_t": arrival,
        }
    return results


def main():
    if len(sys.argv) < 2:
        print("使い方: python3 kagoshima_parser.py YYYYMMDD")
        sys.exit(1)

    date_str = sys.argv[1]
    pdf_bytes = fetch_pdf(date_str)
    prices = extract_prices(pdf_bytes)

    print(f"=== {date_str} 鹿児島市中央卸売市場 中値一覧 ===")
    for item in TARGET_ITEMS.values():
        data = prices.get(item)
        if data:
            print(f"{item}: {data['中値_円per_kg']}円/kg "
                  f"(中値{data['中値_円']}円 / {data['量目_kg']}kg, 産地:{data['産地']}, "
                  f"入荷量{data['入荷量_t']}t)")
        else:
            print(f"{item}: データなし")


if __name__ == "__main__":
    main()
