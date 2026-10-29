"""JR-Central (JR東海) live train feed for the baker.

Source: JR-Central's own 運行情報 site (plain JSON, BOM-prefixed, no key):
    https://traininfo.jr-central.co.jp/zairaisen/data/zaisenichi.json
        train_info[]: {trainnumber: "5031M", delay_lin: "<minutes>",
        linename/nickname/traintype/direction: [{lang, name} multi-lang],
        tostation (destination), position/locationRow/locationCol}

JR-Central uses short line names (東海道線/中央線/関西線/高山線) while
dokorail and jr.suki.ing use the full 本線 names - LINE_NAME_REWRITES maps
them. Even with the rewrite, 東海道本線 exists at three JR companies, so
delays.py scopes the timetable-leg search to the entry's own operator.

The shinkansen page (ti08) builds its data URLs at runtime from a template
with 編成 file names - needs a browser session; deferred.
"""

import json
import re

from . import common

BASE = "https://traininfo.jr-central.co.jp"
ZAISEN_URL = BASE + "/zairaisen/data/zaisenichi.json"

DELAY_THRESHOLD_MIN = 1

# JR-Central short line names -> canonical 本線 names used by dokorail/suki
LINE_NAME_REWRITES = {
    "東海道線": "東海道本線",
    "中央線": "中央本線",
    "関西線": "関西本線",
    "高山線": "高山本線",
    "紀勢線": "紀勢本線",
}

# Tokaido Shinkansen (ti08): positions live in a var/ file polled every
# 2.5s by the page, withdrawn after the last train (~01:45 JST). Confirmed
# via browser XHR: trainLocationInfo.{atStation,betweenStation}.bounds.{1|2}
# -> station slots with trains[] = {trainNumber, train: <kind code>,
# delay: <minutes>}. Kind codes / station indices resolve via common_ja.json.
SHINKANSEN_LOC_URL = BASE + "/shinkansen/var/train_info/train_location_info.json"
SHINKANSEN_COMMON_URL = BASE + "/shinkansen/common/data/common_ja.json"
# 発車順序案内（官方表定時刻 + 停站序列）: departure_info_{station}_{bound}.json
# bound: 1=上り(博多→東京) 2=下り(東京→博多)
SHINKANSEN_DEP_URL = BASE + "/shinkansen/var/train_info/departure_info_{}_{}.json"

# 在来線 position grid. train_info 只给 (locationRow, locationCol, position):
# locationRow 是 status_detail.html 里该线站表的 1-based 序号（站点表按
# kudariJun 升序），locationCol 1=上り/2=下り，position 0=在站/1=站间。
# 没有这两张 master，整份 feed 就没有任何站名，永远无法匹配。
ZAISEN_LINE_MASTER_URL = BASE + "/zairaisen/data/hp_senku_master_ja.json"
ZAISEN_STATION_MASTER_URL = BASE + "/zairaisen/data/hp_eki_master_ja.json"

_ZAISEN_GRID = []  # 进程内缓存: [(lookup, by_name, station_order)]

# module-level cache of common_ja constant (station/train maps), filled by
# fetch_shinkansen; used by fetch_shinkansen_departure to name stations
_COMMON_CONST = {}


def fetch_shinkansen_departure(src, station_code, bound, train_number, kind_code):
    """官方発車順序案内: departure_info_{station}_{bound}.json — 该站该方向
    的全天表定時刻表。返回该车次的 {departure: 表定過站分钟, stations: 后续
    停站序列(站名, 不含报站), terminal: 终点站名}，用于与 suki 时刻表做
    「表定对表定 + 站序一致性」的严格消歧。找不到/失败返回 None。"""
    const = _COMMON_CONST.get(0) or {}
    names = const.get("station") or {}
    try:
        d = common.get_json(
            SHINKANSEN_DEP_URL.format(station_code, bound),
            headers={"Referer": BASE + "/shinkansen/sp/ja/ti04.html"},
            retries=2,
        )
        di = (d or {}).get("departureInfo", {}).get("data", [])
        for rec in di:
            if str(rec.get("trainNumber")) == str(train_number) and str(rec.get("train")) == str(kind_code):
                return {
                    "departure": rec.get("departureTime"),
                    "stations": [names.get(str(s)) for s in (rec.get("stations") or [])],
                    "terminal": names.get(str(rec.get("terminalStation"))),
                }
    except Exception as exc:
        print(f"jrcentral departure {station_code}/{bound}/{train_number}: "
              f"{type(exc).__name__}: {str(exc)[:120]}")
    return None


def _multi_lang(entries, lang="ja"):
    for e in entries or []:
        if e.get("lang") == lang:
            return e.get("name") or ""
    return ""


def _offline_json(src, name):
    try:
        return src.offline_json(name)
    except Exception:
        return None


def _zaisen_grid(src):
    """(lookup, by_name, station_order) from the two zairaisen masters.

    lookup: (line name, section text) -> line code (東海道線 appears twice,
    split 熱海～豊橋 / 豊橋～米原, each with its own station list);
    by_name: line name -> [line codes] for section-less feeds;
    station_order: line code -> station names in kudariJun order, i.e.
    locationRow - 1. Failure returns empty maps (rows keep no station)."""
    if _ZAISEN_GRID:
        return _ZAISEN_GRID[0]
    if src.offline:
        senku = _offline_json(src, "jc-senku-master-ja.json")
        eki = _offline_json(src, "jc-eki-master-ja.json")
    else:
        try:
            senku = common.get_json(ZAISEN_LINE_MASTER_URL)
            eki = common.get_json(ZAISEN_STATION_MASTER_URL, retries=2)
        except Exception as exc:
            print(f"jrcentral masters: {type(exc).__name__}: {str(exc)[:120]}")
            senku = eki = None
    if not isinstance(senku, dict) or not isinstance(eki, dict):
        return ({}, {}, {})
    by_line = {}
    for e in eki.get("lst") or []:
        code = str((e or {}).get("ryokakuSenkuCd") or "")
        if code:
            by_line.setdefault(code, []).append(e)

    def _jun(e):
        try:
            return int(str(e.get("kudariJun") or "0"))
        except ValueError:
            return 0

    station_order = {
        code: [e.get("ekiMei") for e in sorted(entries, key=_jun)]
        for code, entries in by_line.items()
    }
    lookup, by_name = {}, {}
    for s in senku.get("lst") or []:
        code = str((s or {}).get("ryokakuSenkuCd") or "")
        name = (s or {}).get("ryokakuSenkuMei") or ""
        if not code or not name:
            continue
        lookup[(name, (s or {}).get("ryokakuSenkuKaishiShuryoEki") or "")] = code
        by_name.setdefault(name, []).append(code)
    grid = (lookup, by_name, station_order)
    _ZAISEN_GRID.append(grid)
    return grid


def _zaisen_station(grid, line_name, row):
    lookup, by_name, station_order = grid
    name, section = line_name, ""
    m = re.match(r"^(.*?)[(（](.*)[)）]\s*$", line_name)
    if m:
        name, section = m.group(1), m.group(2)
    code = lookup.get((name, section))
    if not code:
        codes = by_name.get(name) or []
        if len(codes) != 1:
            return None  # 東海道線 without its section is two different grids
        code = codes[0]
    stations = station_order.get(code) or []
    try:
        idx = int(str(row))
    except ValueError:
        return None
    if 1 <= idx <= len(stations):
        return stations[idx - 1]
    return None


def fetch_shinkansen(src):
    """Tokaido Shinkansen delayed trains. Empty list outside service hours
    (the var/ file 302s once withdrawn) or when everyone is on time."""
    if src.offline:
        d = _offline_json(src, "jc-sk-loc-day.json")
        common_data = _offline_json(src, "jc-sk-common.json")
    else:
        try:
            d = common.get_json(
                SHINKANSEN_LOC_URL + f"?timestamp={int(common.time.time() * 1000)}",
                headers={"Referer": BASE + "/shinkansen/sp/ja/ti08.html"},
            )
            common_data = common.get_json(SHINKANSEN_COMMON_URL)
        except Exception as exc:
            print(f"jrcentral shinkansen: {type(exc).__name__}: {str(exc)[:120]}")
            return []
    if not isinstance(d, dict) or not isinstance(d.get("trainLocationInfo"), dict):
        return []
    tl = d["trainLocationInfo"]
    const = (common_data or {}).get("constant") or {}
    kind_names = const.get("train") or {}
    station_names = const.get("station") or {}
    _COMMON_CONST[0] = const

    body = []
    seen = set()
    for section in ("atStation", "betweenStation"):
        bounds = (tl.get(section) or {}).get("bounds") or {}
        for bound, slots in bounds.items():
            for slot in slots or []:
                station_code = str(slot.get("station") or "")
                station = station_names.get(station_code)
                # one map covers three lines: 東京..新大阪 / 新神戸..博多 /
                # 新鳥栖..鹿児島中央 (indices from common_ja station map)
                try:
                    idx = int(station_code)
                except ValueError:
                    idx = 0
                line_name = "九州新幹線" if idx >= 46 else ("山陽新幹線" if idx >= 16 else "東海道新幹線")
                railways = {
                    "東海道新幹線": ["JR-Central.TokaidoShinkansen"],
                    "山陽新幹線": ["JR-West.SanyoShinkansen", "JR-Central.TokaidoShinkansen"],
                    "九州新幹線": ["JR-Kyushu.KyushuShinkansen"],
                }.get(line_name, [])
                for tr in slot.get("trains") or []:
                    number = str(tr.get("trainNumber") or "").strip()
                    delay_min = int(tr.get("delay") or 0)
                    if not number or delay_min < DELAY_THRESHOLD_MIN:
                        continue
                    if number in seen:
                        continue
                    seen.add(number)
                    body.append(
                        {
                            "owl:sameAs": f"JR-Central-Shinkansen:{number}",
                            "odpt:railway": f"odpt.Railway:{railways[0] if railways else 'JR-Central'}",
                            "odpt:trainNumber": number,
                            "odpt:delay": delay_min * 60,
                            "odpt:trainType": kind_names.get(str(tr.get("train"))) or None,
                            "_line_ja": line_name,
                            "_suki_line": line_name,
                            "_railways": railways,
                            "_number_prefix": number,
                            "_station": station,
                            "_station_code": station_code,
                            "_bound": bound,
                            "_train_kind": str(tr.get("train")),
                        }
                    )
    return body


def fetch(src=None):
    src = src or common.Source()
    zairaisen = _fetch_zairaisen(src)
    if zairaisen is None:
        return None
    # Tokaido Shinkansen rides in the same operator: service-hours only
    # (the var/ file is withdrawn after the last train), failures tolerated
    try:
        zairaisen["body"].extend(fetch_shinkansen(src))
    except Exception as exc:
        print(f"jrcentral shinkansen merge: {type(exc).__name__}: {str(exc)[:120]}")
    return zairaisen


def _fetch_zairaisen(src):
    src = src or common.Source()
    if src.offline:
        try:
            d = src.offline_json("jc-zaisenichi.json")
        except Exception:
            return None
    else:
        try:
            data = common.http_get(ZAISEN_URL, headers={"Referer": BASE + "/zairaisen/index.html"})
            d = json.loads(data.decode("utf-8-sig"))
        except Exception as exc:
            print(f"jrcentral feed: {type(exc).__name__}: {str(exc)[:120]}")
            return None
    trains = d.get("train_info") if isinstance(d, dict) else None
    if not isinstance(trains, list):
        return None

    body = []
    grid = None
    for t in trains:
        if not isinstance(t, dict):
            continue
        number = str(t.get("trainnumber") or "").strip()
        delay_min = int(str(t.get("delay_lin") or "0").strip() or 0)
        if not number or delay_min < DELAY_THRESHOLD_MIN:
            continue
        raw_name = _multi_lang(t.get("linename"))
        # feed names carry section suffixes: 東海道線(熱海～豊橋) -> 東海道線
        line_name = common.PAREN_RE.sub("", raw_name).strip()
        line_name = LINE_NAME_REWRITES.get(line_name, line_name)
        station = None
        if str(t.get("position") or "") == "0":
            # 在站才有表定過站的时刻锚点；站间(position=1)只知道下一站，
            # 到站表定时刻比 now-delay 晚 1-5 分钟不定，宁漏不误报
            if grid is None:
                grid = _zaisen_grid(src)
            station = _zaisen_station(grid, raw_name, t.get("locationRow"))
        body.append(
            {
                "owl:sameAs": f"JR-Central:{number}.{t.get('train_identification_key', '')}",
                "odpt:railway": "odpt.Railway:JR-Central",
                "odpt:trainNumber": number,
                "odpt:delay": delay_min * 60,
                "odpt:trainType": _multi_lang(t.get("traintype")) or None,
                "_line_ja": line_name,
                "_suki_line": line_name,
                "_station": station,
                "_dest": _multi_lang(t.get("tostation")) or None,
                # 站名来自站表序号、只有 now-delay 一个时刻锚点：dokorail
                # 车段按站+时刻兜底(tol=20)对通勤线太松，交给 suki-direct
                # 的 ±3 唯一性收敛
                "_skip_position_leg": bool(station),
                "_nickname": _multi_lang(t.get("nickname")) or None,
            }
        )
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "source_time": d.get("create_time"),
        "body": body,
        "source": "traininfo.jr-central.co.jp",
    }

