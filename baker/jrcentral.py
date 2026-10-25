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


def _multi_lang(entries, lang="ja"):
    for e in entries or []:
        if e.get("lang") == lang:
            return e.get("name") or ""
    return ""


def fetch(src=None):
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
