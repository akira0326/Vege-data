"""
全国主要市場 青果物市況情報 取得モジュール（無料・登録不要ルート）

データ元: 農林水産省「青果物市況情報」
取得経路: cultivationdata.net の無料Web API（農水省データのミラー、JSON/CSV、海外サーバー可）
          https://api.cultivationdata.net/mcdata?mc={市場コード}&cat=v&type=csv
更新:     毎日17時頃

返すレコード形式（既存パイプラインに合わせやすい辞書）:
  {
    "date": "2026/03/26",      # データ日付（文字列）
    "market": "大田",            # 市場名
    "market_code": "13310",
    "item": "ごぼう",            # 品目名
    "origin": "青森",            # 産地名
    "volume_t": 12.3,           # 入荷量(t)  ※産地内の規格を合算
    "high": 520,                # 高値(円/kg) ※入荷量で加重平均
    "mid": 430,                 # 中値(円/kg) ※入荷量で加重平均
    "low": 360,                 # 安値(円/kg) ※入荷量で加重平均
  }

設計メモ:
 - 1リクエスト＝1市場（cat=v で野菜全品目）。対象3品目は品目名で絞るため品目コード不要。
   市場×品目で何度も叩かず、過剰アクセスを避ける。
 - 同一(市場,品目,産地)に等級・階級違いの複数行があるため、入荷量で加重平均して
   産地ごとに高値・中値・安値を1組に集約する（中値の定義＝販売量最多、に整合）。
 - 価格は円/kg（軽減税率対象のため税込8%）。数量はt。
"""

import csv
import io
import time
import urllib.request

API_BASE = "https://api.cultivationdata.net/mcdata"

# 対象品目（品目名で照合）。表記ゆれに備え別名も許容。
TARGET_ITEMS = {
    "ごぼう": "ごぼう",
    "さつまいも": "さつまいも",
    "かんしょ": "さつまいも",   # 別表記の保険
    "さといも": "さといも",
}

# 対象市場（4エリア）。コードは青果物市況情報API説明書より。
# 必要に応じて増減してください。鹿児島市場(46300)もここに含められます。
MARKETS = {
    # 関東
    "13310": "東京大田",
    "30100": "東京豊洲",
    "14300": "横浜本場",
    # 関西
    "27300": "大阪本場",
    "26300": "京都市中央",
    "28300": "神戸本場",
    # 中国
    "34300": "広島中央",
    # 九州
    "40320": "福岡市中央",
    "40300": "北九州市中央",
    "46300": "鹿児島市中央",   # 産地市場。宮崎はこのソースに無いため別途スクレイパー併用。
}

# 市場→エリアの対応（集計・レポート用）
MARKET_AREA = {
    "13310": "関東", "30100": "関東", "14300": "関東",
    "27300": "関西", "26300": "関西", "28300": "関西",
    "34300": "中国",
    "40320": "九州", "40300": "九州", "46300": "九州",
}


def _to_float(s):
    if s is None:
        return None
    s = s.strip()
    if s == "" or s.lower() == "nan":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_market_csv(market_code: str, timeout: int = 30) -> str:
    """1市場分の野菜CSVテキストを取得して返す"""
    url = f"{API_BASE}?mc={market_code}&cat=v&type=csv"
    req = urllib.request.Request(url, headers={"User-Agent": "vege-data-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def parse_market_csv(csv_text: str):
    """
    CSVテキストを (市場,品目,産地) ごとに集約したレコードのリストへ変換する。
    対象品目(TARGET_ITEMS)のみ抽出。
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    # (市場コード,市場名,品目名,産地名) -> 累積
    agg = {}
    for row in reader:
        raw_item = (row.get("品目名") or "").strip()
        item = TARGET_ITEMS.get(raw_item)
        if item is None:
            continue

        vol = _to_float(row.get("入荷量(t)"))
        mid = _to_float(row.get("中値(円)"))
        high = _to_float(row.get("高値(円)"))
        low = _to_float(row.get("安値(円)"))
        if vol is None or vol <= 0:
            # 加重に使えない行はスキップ（数量不明）
            continue

        key = (
            (row.get("市場コード") or "").strip(),
            (row.get("市場名") or "").strip(),
            item,
            (row.get("産地名") or "").strip(),
        )
        a = agg.setdefault(key, {
            "date": (row.get("日付") or "").strip(),
            "vol": 0.0,
            "mid_w": 0.0, "mid_v": 0.0,
            "high_w": 0.0, "high_v": 0.0,
            "low_w": 0.0, "low_v": 0.0,
        })
        a["vol"] += vol
        if mid is not None:
            a["mid_w"] += mid * vol; a["mid_v"] += vol
        if high is not None:
            a["high_w"] += high * vol; a["high_v"] += vol
        if low is not None:
            a["low_w"] += low * vol; a["low_v"] += vol

    records = []
    for (mcode, mname, item, origin), a in agg.items():
        def wavg(w, v):
            return round(w / v) if v > 0 else None
        records.append({
            "date": a["date"],
            "market": mname,
            "market_code": mcode,
            "area": MARKET_AREA.get(mcode, ""),
            "item": item,
            "origin": origin,
            "volume_t": round(a["vol"], 1),
            "high": wavg(a["high_w"], a["high_v"]),
            "mid": wavg(a["mid_w"], a["mid_v"]),
            "low": wavg(a["low_w"], a["low_v"]),
        })
    return records


def fetch_all(markets: dict = None, polite_sec: float = 1.0):
    """全対象市場を順に取得し、産地別レコードをまとめて返す"""
    markets = markets or MARKETS
    out = []
    for code in markets:
        try:
            text = fetch_market_csv(code)
            recs = parse_market_csv(text)
            out.extend(recs)
        except Exception as e:
            print(f"[警告] 市場 {code} 取得失敗: {e}")
        time.sleep(polite_sec)  # 過剰アクセス回避
    return out


if __name__ == "__main__":
    rows = fetch_all()
    print(f"取得レコード数: {len(rows)}")
    for r in rows[:20]:
        print(r)

