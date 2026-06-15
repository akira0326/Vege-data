"""
宮崎中央青果の「青果物市況調査票（野菜）」PDFから、
指定品目の中値を円/kgに換算して抽出するスクリプト。

PDFの構成:
  1ページに左右2ブロック(各10列)が並ぶ:
    [品目, 規格, 産地名, 入荷量, 高値, 中値, 安値, 量目, 規格2, 概況]
  品目名が空欄(None)の行は直前行と同じ品目の別産地。
  中値が "～"（チルダ）の場合はデータなし→高値・安値の平均で代用。

使い方:
  python3 miyazaki_parser.py /path/to/青果物市況野菜-X.pdf
"""

import sys
import re
import pdfplumber

TARGET_ITEMS = {
    "甘藷": "さつまいも",
    "里芋": "さといも",
    "牛蒡": "ごぼう",
    "キャベツ": "キャベツ",
    "人参": "にんじん",
    "レタス": "レタス",
    "白菜": "はくさい",
    "玉葱": "たまねぎ",
    "馬鈴薯": "じゃがいも",
}

NODATA_MARKS = ("", "～", "-", "ー", "−", None)


def to_number(s):
    if s in NODATA_MARKS:
        return None
    s = str(s).strip().replace(",", "")
    s = re.sub(r"[^\d.]", "", s)
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_kg(ryome_str):
    """'10kg' '5ｋｇ' '15kg' '0.2k' などをkg数値に変換"""
    if not ryome_str:
        return None
    s = str(ryome_str)
    s = s.replace("ｋｇ", "kg").replace("Kg", "kg").replace("KG", "kg")
    s = s.replace("㎏", "kg")
    m = re.search(r"([\d.]+)\s*k(g)?", s, re.IGNORECASE)
    if m:
        return float(m.group(1))
    # '0.2k' のようなパターン
    m = re.search(r"([\d.]+)k", s)
    if m:
        return float(m.group(1))
    return None


def to_arrival(s):
    """'5ｔ' '0.4ｔ' '2.6ｔ' -> トン数値"""
    if not s:
        return 0.0
    s = str(s).replace("ｔ", "t")
    m = re.search(r"([\d.]+)\s*t", s)
    if m:
        return float(m.group(1))
    return 0.0


def parse_block(rows, col_offset):
    """
    col_offset: 0 = 左ブロック(列0-9), 10 = 右ブロック(列10-19)
    戻り値: {表示名: [(arrival, chuune, ryome_kg, sanchi), ...]}
    """
    candidates = {v: [] for v in TARGET_ITEMS.values()}
    last_item = None

    for row in rows:
        c = row[col_offset:col_offset + 10]
        if len(c) < 10:
            continue
        item_raw = (c[0] or "").strip()

        matched = None
        if item_raw in TARGET_ITEMS:
            matched = TARGET_ITEMS[item_raw]
            last_item = matched
        elif item_raw == "" and last_item is not None:
            matched = last_item
        else:
            last_item = None
            continue

        sanchi = (c[2] or "").strip()
        arrival = to_arrival(c[3])
        takane = to_number(c[4])
        chuune = to_number(c[5])
        yasune = to_number(c[6])
        ryome_kg = to_kg(c[7])

        if ryome_kg is None:
            continue

        if chuune is None:
            vals = [v for v in (takane, yasune) if v is not None]
            if not vals:
                continue
            chuune = sum(vals) / len(vals)

        candidates[matched].append((arrival, chuune, ryome_kg, sanchi))

    return candidates


def extract_prices(pdf_path: str) -> dict:
    all_candidates = {v: [] for v in TARGET_ITEMS.values()}

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                left = parse_block(table, 0)
                right = parse_block(table, 10)
                for d in (left, right):
                    for item, rows in d.items():
                        all_candidates[item].extend(rows)

    results = {}
    for item, rows in all_candidates.items():
        if not rows:
            results[item] = None
            continue
        with_arrival = [r for r in rows if r[0] > 0]
        best = max(with_arrival or rows, key=lambda r: r[0])
        arrival, chuune, ryome, sanchi = best
        results[item] = {
            "中値_円": chuune,
            "量目_kg": ryome,
            "中値_円per_kg": round(chuune / ryome, 1),
            "産地": sanchi,
            "入荷量_t": arrival,
        }
    return results


def fetch_miyazaki_pdf_path(today):
    """
    宮崎中央青果のページから、指定日の「{YYYYMMDD}野菜」PDFのURLを探し、
    一時ファイルにダウンロードしてそのパスを返す。見つからなければ None。
    （main.py 側が直近7日を遡って呼び出します）
    """
    import requests

    list_url = "https://www.miyaseiren.com/shikyo_data/category/chuou/"
    resp = requests.get(list_url, timeout=30)
    resp.raise_for_status()
    html = resp.text

    date_str = today.strftime("%Y%m%d")
    pattern = rf'{date_str}野菜.*?href="(https://[^"]+?\.pdf)"'
    m = re.search(pattern, html, re.DOTALL)
    if not m:
        return None

    pdf_url = m.group(1)
    pdf_resp = requests.get(pdf_url, timeout=30)
    pdf_resp.raise_for_status()

    tmp_path = f"/tmp/miyazaki_{date_str}.pdf"
    with open(tmp_path, "wb") as f:
        f.write(pdf_resp.content)
    return tmp_path


def main():
    if len(sys.argv) < 2:
        print("使い方: python3 miyazaki_parser.py /path/to/file.pdf")
        sys.exit(1)

    pdf_path = sys.argv[1]
    prices = extract_prices(pdf_path)

    print("=== 宮崎中央青果 中値一覧 ===")
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

