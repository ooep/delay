"""Enoden (江ノ島電鉄) live delay feed.

Source: the NEC-built position site https://www.enoden-location.jp/
(/Home/Index?mode=1, mobile mode). The page itself polls every 3 s:

    GET  /Home/TrainList?mode=1     -> JSON array (vehicle positions)
    POST /Home/web01Info (lang=ja)  -> {"status","teropLevel","teropMessage"}
    GET  /Home/web01Unkyu?mode=1    -> suspension markers (usually [])

Train row: {adjustment, retsuBng:" 229", usF:0|1, point_x, point_y,
            main:{no,type}|null, sub:{no,type}|null}
  retsuBng = 列番 (with a leading space), usF 1=U(上り 藤沢方面) 0=D(下り
  鎌倉方面), point_x/y = pixel position on /img/train_m/smart_jp.png
  (1627x9176; y small = 藤沢 top, y large = 鎌倉 bottom). 列番 parity
  matches direction (odd = 下り, even = 上り) on every captured sample.

Delay delivery (decided 2026-10-28 after two live captures: 10-27 19:1x
JST with the 車両点検 delay ticker, and 10-28 08:0x JST with an empty
ticker):
  * The site exposes NO per-train delay field - only position + 列番 + a
    line-level free-text ticker. A position-vs-timetable per-train
    estimate is bimodal (an on-time train snapshotted just before its
    scheduled arrival reads 0..headway minutes late), so per-train delay
    numbers are NOT derived (宁漏不误报, do not fabricate).
  * The operator's own ticker is the delay signal: when teropMessage
    contains a delay keyword (遅れ/遅延/見合わせ/... and not 通常) the
    line is officially delayed -> emit ONE line-level entry, OsakaMetro
    precedent, with the fixed line-level delay and the ticker text kept
    verbatim as the cause. Normal / empty ticker -> body=[] (宁漏).
  * The parsed vehicle snapshot (列番/方向/站) is attached to the payload
    under "vehicles" for future per-train work (map display etc.); the
    bake only consumes "body".
"""

import json
import urllib.request

from . import common

TRAINLIST = "https://www.enoden-location.jp/Home/TrainList"
INFO = "https://www.enoden-location.jp/Home/web01Info"
REFERER = "https://www.enoden-location.jp/Home/Index?mode=1"

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 "
      "Safari/604.1")

# line-level convention, same as OsakaMetro's line-level operation status
LINE_LEVEL_DELAY_SECONDS = 5 * 60

# station y-centers on smart_jp.png (from the page's map area coords).
# Names use the suki tmaps normalized forms (ケ not ヶ, 長谷(神奈川県)).
STATION_Y = [
    ("藤沢", 31), ("石上", 155), ("柳小路", 277), ("鵠沼", 401),
    ("湘南海岸公園", 524), ("江ノ島", 649), ("腰越", 771),
    ("鎌倉高校前", 895), ("七里ケ浜", 1016), ("稲村ケ崎", 1140),
    ("極楽寺", 1265), ("長谷(神奈川県)", 1388), ("由比ケ浜", 1511),
    ("和田塚", 1635), ("鎌倉", 1756),
]


def _headers():
    return {
        "User-Agent": UA,
        "Referer": REFERER,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }


def _get(url, data=None, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            hdrs = _headers()
            if data is not None:
                hdrs["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
                body = data.encode("utf-8")
            else:
                body = None
            req = urllib.request.Request(url, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as exc:
            last = exc
            if attempt < retries:
                import time
                time.sleep(1.5)
    raise last


def _snap_station(y):
    """Nearest station name (suki norm form) for a TrainList point_y."""
    best, best_d = None, 10 ** 9
    for name, cy in STATION_Y:
        d = abs(y - cy)
        if d < best_d:
            best, best_d = name, d
    return best


def _parse_vehicles(rows):
    """TrainList rows -> [{列番, 方向 U/D, 站}]; skips the adjustment
    banner row (rows[0]) and malformed records."""
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if r.get("adjustment") is not None and "retsuBng" not in r:
            continue  # banner row
        retsu = str(r.get("retsuBng") or "").strip()
        usf = r.get("usF")
        py = r.get("point_y")
        if not retsu or usf not in (0, 1) or not isinstance(py, (int, float)):
            continue
        # 列番 parity must agree with the direction flag (odd=下り D,
        # even=上り U) - every captured sample is consistent; skip odd ones
        try:
            odd = int(retsu) % 2 == 1
        except ValueError:
            continue
        direction = "D" if odd else "U"
        if (usf == 0) != odd:
            continue  # inconsistent feed record
        out.append({
            "列番": retsu,
            "direction": direction,
            "station": _snap_station(py),
        })
    return out


def _ticker_delayed(info):
    """terop info -> bool. Delay keywords on the operator's own ticker;
    通常/平常/正常 negate (e.g. "遅れが解消し、通常運転に戻りました")."""
    msg = str(info.get("teropMessage") or "")
    if not msg:
        return False
    if any(k in msg for k in ("通常", "平常", "正常")):
        return False
    return any(k in msg for k in (
        "遅れ", "遅延", "見合わせ", "見合せ", "運転中止", "抑止", "折返し運転",
        "大幅に乱れ",
    ))


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        try:
            raw = src.offline.root.joinpath("enoden-trainlist.json").read_text(
                encoding="utf-8")
        except Exception:
            raw = "[]"
        try:
            info = json.loads(src.offline.root.joinpath(
                "enoden-web01info.json").read_text(encoding="utf-8"))
        except Exception:
            info = {}
    else:
        raw = _get(f"{TRAINLIST}?mode=1")
        try:
            info = json.loads(_get(INFO, data="lang=ja"))
        except Exception as exc:
            print(f"enoden web01Info: {type(exc).__name__}: {str(exc)[:100]}")
            info = {}
    try:
        rows = json.loads(raw)
    except Exception as exc:
        print(f"enoden trainlist: {type(exc).__name__}: {str(exc)[:100]}")
        return None
    vehicles = _parse_vehicles(rows)
    body = []
    if _ticker_delayed(info):
        # OsakaMetro precedent: line-level entry, cause = operator's own text
        body.append({
            "owl:sameAs": "Enoden:line",
            "odpt:railway": "odpt.Railway:Enoden.Enoden",
            "odpt:trainNumber": None,
            "odpt:delay": LINE_LEVEL_DELAY_SECONDS,
            "odpt:trainType": None,
            "_line_ja": "江ノ島電鉄線",
            "_suki_line": "江ノ島電鉄線",
            "_enoden_status": "delay",
            "_enoden_status_text": str(info.get("teropMessage") or ""),
            "_enoden_terop_level": info.get("teropLevel"),
        })
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "vehicles": vehicles,
        "ticker": info.get("teropMessage") or "",
        "source": "www.enoden-location.jp",
    }
