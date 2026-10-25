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
    for t in trains:
        if not isinstance(t, dict):
            continue
        number = str(t.get("trainnumber") or "").strip()
        delay_min = int(str(t.get("delay_lin") or "0").strip() or 0)
        if not number or delay_min < DELAY_THRESHOLD_MIN:
            continue
        line_name = _multi_lang(t.get("linename"))
        # feed names carry section suffixes: 東海道線(熱海～豊橋) -> 東海道線
        line_name = common.PAREN_RE.sub("", line_name).strip()
        line_name = LINE_NAME_REWRITES.get(line_name, line_name)
        body.append(
            {
                "owl:sameAs": f"JR-Central:{number}.{t.get('train_identification_key', '')}",
                "odpt:railway": "odpt.Railway:JR-Central",
                "odpt:trainNumber": number,
                "odpt:delay": delay_min * 60,
                "odpt:trainType": _multi_lang(t.get("traintype")) or None,
                "_line_ja": line_name,
                "_suki_line": line_name,
                "_station": None,
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


import json  # noqa: E402
