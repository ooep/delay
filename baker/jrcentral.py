"""JR-Central (JR東海) live train feed for the baker.

Source: JR-Central's own 運行情報 site (plain JSON, BOM-prefixed, no key):
    https://traininfo.jr-central.co.jp/zairaisen/data/zaisenichi.json
        train_info[]: {trainnumber: "5031M", delay_lin: "<minutes>",
        linename/nickname/traintype/direction: [{lang, name} multi-lang],
        tostation (destination), position/locationRow/locationCol}
    https://traininfo.jr-central.co.jp/zairaisen/data/kobetsujikoku.json
        the same trains' official timetables (per stop: HHMM + 停車/通過),
        joined by (train_identification_key, trainnumber) - supplies the
        scheduled minute and downstream stop sequence used as the matching
        anchor for position-only rows.

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
# 個別列車時刻表（当日全社 ~2300 本、gzip 1.5MB / 解压 24MB）:
# train_identification_key 与 zaisenichi 的 train_info 逐条对应，每站带
# 表定時刻（HHMM，可 >24h）与停車/通過区分。站间车只有 position 格、
# 不知道下一站，必须靠它拿「该车自己的」表定发车分钟和后续停站序列。
ZAISEN_TIMETABLE_URL = BASE + "/zairaisen/data/kobetsujikoku.json"

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


def _official_timetables(src, keys):
    """{(train_identification_key, trainnumber): record} for `keys` only.

    The identification key alone is NOT unique: a through working split at a
    crew/line boundary keeps one key for two train numbers (325C 松阪→多気
    and 325D 相可→新宮 share a key), so records are addressed by key+number.
    kobetsujikoku.json has no per-train variant - the whole file is fetched
    and narrowed to the handful of delayed trains. Failures return {}: rows
    keep the plain position fallbacks."""
    if not keys:
        return {}
    if src.offline:
        d = _offline_json(src, "jc-kobetsujikoku.json")
    else:
        try:
            d = common.get_json(ZAISEN_TIMETABLE_URL, retries=2)
        except Exception as exc:
            print(f"jrcentral kobetsujikoku: {type(exc).__name__}: {str(exc)[:120]}")
            d = None
    if not isinstance(d, dict):
        return {}
    out = {}
    for t in d.get("train_info") or []:
        k = str((t or {}).get("train_identification_key") or "")
        if k and k in keys:
            out[(k, str((t or {}).get("trainnumber") or ""))] = t
    return out


def _hhmm(v):
    """'HHMM' (hours may exceed 23) -> minute of day, or None."""
    v = str(v or "").strip()
    if len(v) < 3:
        return None
    try:
        return int(v[:-2]) * 60 + int(v[-2:])
    except ValueError:
        return None


def _official_stops(rec):
    """[(station, arrival_min, departure_min)] for the stops this train
    actually makes (stopthroughkbn 1), in stopStationOrder order.
    通過站带空时刻、也不存在于 suki 的停站模式里,锚在它们身上永远解不出，
    所以直接剔除。"""
    rows = []
    for s in rec.get("timetable") or []:
        if not isinstance(s, dict):
            continue
        if str(s.get("stopthroughkbn") or "1") != "1":
            continue
        name = _multi_lang(s.get("station"))
        if not name:
            continue
        arr = _hhmm(s.get("arrivalTime1"))
        if arr is None:
            arr = _hhmm(s.get("arrivalTime2"))
        dep = _hhmm(s.get("departureTime1"))
        if dep is None:
            dep = _hhmm(s.get("departureTime2"))
        if arr is None and dep is None:
            continue
        try:
            order = int(str(s.get("stopStationOrder") or "0"))
        except ValueError:
            order = 0
        rows.append((order, name, arr, dep))
    rows.sort(key=lambda r: r[0])
    return [(name, arr, dep) for _, name, arr, dep in rows]


def _official_anchor(stops, station, est):
    """Index of the stop to anchor on, or None.

    `station` (在站位置表解出的站名) 优先;它不在停站序列里(通過站)时退回
    「表定发车不晚于 est 的最后一站」。锚在哪一站用的都是这张表里同一列
    车自己的表定分钟，所以站间车不知道刚驶离/即将到达也不影响锁定，
    只影响展示的 from 站(取已驶离站，宁保守)。"""
    if station:
        for i, (name, _, _) in enumerate(stops):
            if name == station:
                return i
    if est is None:
        return None
    best = None
    for i, (_, arr, dep) in enumerate(stops):
        m = dep if dep is not None else arr
        if m is not None and m <= est + 1:
            best = i
    return best


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
    pairs = []
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
        entry = {
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
        body.append(entry)
        pairs.append((entry, t))

    # 官方個別時刻表: 给每列车一个「它自己的」表定发车分钟 + 后续停站序列。
    # 站间车靠这个才有锚点（位置格只能猜下一站），在站车也换成表定时刻，
    # 比 now-delay 精确一个 feed 刷新周期。
    now = common.jst_now()
    now_min = now.hour * 60 + now.minute
    official = _official_timetables(
        src, {str(t.get("train_identification_key") or "") for _, t in pairs}
    )
    for e, t in pairs:
        rec = official.get((
            str(t.get("train_identification_key") or ""),
            str(t.get("trainnumber") or ""),
        ))
        if not rec:
            continue
        stops = _official_stops(rec)
        if len(stops) < 2:
            continue
        est = now_min - int(round((e.get("odpt:delay") or 0) / 60))
        ai = _official_anchor(stops, e.get("_station"), est)
        if ai is not None and e.get("_station") == stops[ai][0]:
            m = stops[ai][2] if stops[ai][2] is not None else stops[ai][1]
            if m is None or abs(m - est) > 15:
                # 位置表解出的站在官方时刻上离 est 太远：多半是线路段
                # 解错了（同名两段），退回表定时刻定锚
                ai = _official_anchor(stops, None, est)
        if ai is None:
            continue
        anchor_min = stops[ai][2] if stops[ai][2] is not None else stops[ai][1]
        tail = stops[ai + 1:]
        if anchor_min is None or not tail:
            continue  # 已在终点 / 无表定時刻，无从匹配
        e["_station"] = stops[ai][0]
        e["_official"] = {
            "departure": anchor_min,
            "stations": [s[0] for s in tail],
            "terminal": _multi_lang(rec.get("terminalStation")) or tail[-1][0],
        }
        e["_skip_position_leg"] = True

    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "source_time": d.get("create_time"),
        "body": body,
        "source": "traininfo.jr-central.co.jp",
    }

