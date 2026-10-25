"""Nankai (南海電鉄) live train feed for the baker.

Source: the Nankai app's public JSON (no key, no WAF observed):
    https://external-data.nankaiapp.com/tid/trains.json   trains in transit
    https://external-data.nankaiapp.com/line/lines.json   line/station master

Train record: {train_number: "6920", delay: <minutes>, line_id, direction:
"up"|"down", station_id, next_station_id, position: <0-200 segment
progress>, train_kind_id, destination_station_id, car_numbers}.

line_id 1 = 南海線 (their combined name for 本線+空港線), 2 = 高野線,
3/4 = the 支線 twins of those; mapped onto dokorail's Nankai.* railways.
Delays are whole minutes; numbers are bare digits, so the LegStore prefix
match covers both bare and (hypothetically) suffixed dokorail numbers.
"""

import json

from . import common

BASE = "https://external-data.nankaiapp.com"
TRAINS_URL = BASE + "/tid/trains.json"
LINES_URL = BASE + "/line/lines.json"

DELAY_THRESHOLD_MIN = 1

# nankai line_id -> (board line name, dokorail railways for timetable legs)
LINE_MAP = {
    1: ("南海本線", ["Nankai.Main", "Nankai.AirportLineRailways"]),
    2: ("高野線", ["Nankai.Koya", "Nankai.Semboku"]),
    3: ("南海本線", ["Nankai.Main"]),   # 支線 twin of line 1
    4: ("高野線", ["Nankai.Koya"]),     # 支線 twin of line 2 (汐見橋)
}


def _offline_json(src, name):
    try:
        return src.offline_json(name)
    except Exception:
        return None


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        trains = (_offline_json(src, "nk-trains.json") or {}).get("trains") or []
        lines_master = (_offline_json(src, "nk-lines.json") or {}).get("lines") or []
    else:
        try:
            trains = (common.get_json(TRAINS_URL) or {}).get("trains") or []
            lines_master = (common.get_json(LINES_URL) or {}).get("lines") or []
        except Exception as exc:
            print(f"nankai feed: {type(exc).__name__}: {str(exc)[:120]}")
            return None
    station_names = {}
    for line in lines_master:
        for st in line.get("stations") or []:
            station_names[st.get("id")] = st.get("name_ja")

    body = []
    for t in trains:
        if not isinstance(t, dict):
            continue
        number = t.get("train_number")
        delay_min = int(t.get("delay") or 0)
        if not number or delay_min < DELAY_THRESHOLD_MIN:
            continue
        line_id = t.get("line_id")
        line_name, railways = LINE_MAP.get(line_id, (f"nankai:{line_id}", []))
        body.append(
            {
                "owl:sameAs": f"Nankai:{line_id}.{number}",
                "odpt:railway": "odpt.Railway:Nankai",
                "odpt:trainNumber": str(number),
                "odpt:delay": delay_min * 60,
                "_number_prefix": str(number),
                "_railways": railways,
                "_line_ja": line_name,
                "_suki_line": line_name,
                "_station": station_names.get(t.get("station_id")),
                "_direction": t.get("direction"),
            }
        )
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "external-data.nankaiapp.com",
    }
