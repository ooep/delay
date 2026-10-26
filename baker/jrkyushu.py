"""JR-Kyushu (九州) live train feed for the baker.

Source: the goto-mirai web app (https://www.goto-mirai.jrkyushu.co.jp/),
plain JSON, no key, Akamai-fronted (be polite: serial requests, Referer
set; parallel scans trip an edge ban):
    /api/common/trainPosition?drivingRouteCode=C&upperLowerKbn=1|2
                            &stationCode=S&isVisibleAllTrain=true&lang=ja
    /api/station/headerData?drivingRouteCode=C&stationCode=S&lang=ja

Response: {directionName, currentTimeStamp, trainPositionList: [
  {id, stationInfo: [{stationCode, stationName, routeSymbol}],
   trainPositionList: [{trainNumber, trainKindInformationName,
                        trainKindInformationCode, delayMinutes,
                        roundedDelayMinutes, orMoreDelayMinutes}]}]}
- position = station-slot style: a train sits in the slot of its
  current/approaching station (like JR-West's train guide).
- delay: delayMinutes plus two flags - orMoreDelayMinutes ("+N分以上",
  a floor, not a cap) and roundedDelayMinutes ("約N分").
- trainNumber is a bare int ("3222"); dokorail bundles suffix letters
  ("3222M"/"520D"), so leg lookup uses number-prefix matching
  (delays.LegStore, prefix mode).
- upperLowerKbn 1/2 = the two directions; a train crossing a section
  boundary (600/601 both = 鹿児島本線) appears under both codes, so
  records are de-duplicated per train number (max delay kept).
- trainPosition validates that stationCode belongs to the route
  (PIME0002 otherwise); headerData is lenient and is the route-name
  discovery endpoint (scan codes serially to extend ROUTES).
"""

from . import common

BASE = "https://www.goto-mirai.jrkyushu.co.jp"
TRAIN_POS_URL = BASE + "/api/common/trainPosition"

# (drivingRouteCode, line name, a stationCode on that route, dokorail
# railway ids for timetable legs). stationCode anchors the "selected
# station" and MUST belong to the route (PIME0002 otherwise). Known pairs
# discovered so far; extend by scanning headerData for codes (lenient
# endpoint), then finding one station of the route via the app or the
# route's own response (it lists all its stations with codes).
ROUTES = [
    ("50", "長崎本線", "91120060",
     ["JR-Kyushu.NagasakiMain"]),
    ("100", "日豊本線", "91401250",
     ["JR-Kyushu.Nippo", "JR-Kyushu.MiyazakiKuko"]),
    ("130", "久大本線", "91101380",
     ["JR-Kyushu.KyudaiMain"]),
    ("170", "豊肥本線", "91301210",
     ["JR-Kyushu.HohiMain"]),
    ("200", "指宿枕崎線", "91401250",
     ["JR-Kyushu.IbusukiMakurazaki"]),
    ("210", "宮崎空港線", "91405040",
     ["JR-Kyushu.MiyazakiKuko"]),
    ("220", "日南線", "91405040",
     ["JR-Kyushu.Nichinan"]),
    ("312", "九州新幹線", "91401250",
     ["JR-Kyushu.KyushuShinkansen"]),
    ("320", "西九州新幹線", "91120320",
     ["JR-Kyushu.NishiKyushuShinkansen"]),
    ("500", "福北ゆたか線", "91101270",
     ["JR-Kyushu.FukuhokuYutaka", "JR-Kyushu.ChikuhoMain"]),
    ("60", "佐世保線", "91120060",
     ["JR-Kyushu.Sasebo"]),
    ("70", "大村線", "91120060",
     ["JR-Kyushu.OmuraLineHaikiIsahaya"]),
    ("80", "唐津線", "91101270",
     ["JR-Kyushu.Karatsu"]),
    ("90", "筑肥線", "91101270",
     ["JR-Kyushu.Chikuhi"]),
    ("91", "筑肥西線", "91101270",
     ["JR-Kyushu.Chikuhi"]),
    ("160", "後藤寺線", "91101270",
     ["JR-Kyushu.Gotoji"]),
    ("600", "鹿児島本線", "91101380",
     ["JR-Kyushu.KagoshimaMain", "JR-Kyushu.KagoshimaMainSouth"]),
    ("601", "鹿児島本線", "91401250",
     ["JR-Kyushu.KagoshimaMainSouth", "JR-Kyushu.KagoshimaMain"]),
]

DELAY_THRESHOLD_MIN = 1


def _get(url):
    return common.get_json(url, headers={"Referer": BASE + "/separate-page?screen=2"})


def parse_raw(routes_data, now=None):
    """[(code, line_name, payload, dokorail_railways)] -> dokorail-feed-
    shaped payload. body entries carry _number_prefix (bare trainNumber as
    string, for LegStore prefix matching against dokorail's suffixed
    numbers like 3222M/520D) and _railways (dokorail bundles to search)."""
    now = now or common.jst_now()
    body = []
    by_number = {}
    for code, line_name, payload, railways in routes_data:
        if not payload:
            continue
        for slot in payload.get("trainPositionList") or []:
            station_names = [
                st.get("stationName") for st in (slot.get("stationInfo") or [])
            ]
            for tr in slot.get("trainPositionList") or []:
                number_int = tr.get("trainNumber")
                if number_int is None:
                    continue
                number = str(number_int)
                delay_min = int(tr.get("delayMinutes") or 0)
                rec = {
                    "owl:sameAs": f"JR-Kyushu:{code}.{number}",
                    "odpt:railway": "odpt.Railway:JR-Kyushu",
                    "odpt:trainNumber": number,
                    "odpt:delay": delay_min * 60,
                    "odpt:trainType": tr.get("trainKindInformationName"),
                    "_number_prefix": number,
                    "_railways": railways,
                    "_line_ja": line_name,
                    "_direction": payload.get("directionName"),
                    "_or_more": bool(tr.get("orMoreDelayMinutes")),
                    "_rounded": bool(tr.get("roundedDelayMinutes")),
                    "_station": station_names[0] if station_names else None,
                    "_code": code,
                }
                prev = by_number.get(number)
                if prev is not None:
                    # same train seen on an adjacent section: keep max delay
                    if delay_min * 60 > prev["odpt:delay"]:
                        prev["odpt:delay"] = delay_min * 60
                        for k in ("_line_ja", "_station", "_direction", "_or_more", "_rounded", "_railways"):
                            prev[k] = rec[k]
                    continue
                by_number[number] = rec
                body.append(rec)
    return {
        "ok": True,
        "fetchedAt": now.isoformat(),
        "source_time": max(
            (str(p.get("currentTimeStamp")) for _, _, p, *_ in routes_data if p.get("currentTimeStamp")),
            default=None,
        ) or None,
        "body": body,
        "source": "goto-mirai.jrkyushu.co.jp",
    }


def fetch(src=None):
    """Feed-shaped payload, or None when unreachable."""
    src = src or common.Source()
    if src.offline:
        p = src.offline.root / "jrk-routes.json"
        if not p.exists():
            return None
        routes = json.loads(p.read_text())
    else:
        routes = []
        for code, line_name, station, railways in ROUTES:
            try:
                payload = _get(
                    f"{TRAIN_POS_URL}?drivingRouteCode={code}&upperLowerKbn=2"
                    f"&stationCode={station}&isVisibleAllTrain=true&lang=ja"
                )
            except Exception:
                continue
            if isinstance(payload, dict) and payload.get("trainPositionList"):
                routes.append((code, line_name, payload, railways))
    if not routes:
        return None
    return parse_raw(routes)


import json  # noqa: E402  (kept at bottom to keep the header readable)
