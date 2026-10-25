"""Keikyu (京浜急行) live train feed via their web 在線 page API.

Source: https://app-kq.net/web/jp/html/zaisen.html - the KEIKYU app's web
view; its backend is plain JSON, no key:
    https://app-kq.net/api/train             trains in transit
    https://app-kq.net/api/serviceStatus/ekitan  service status

Train record: {position: "ED020", train_no: "1131" ("0" = not published,
e.g. deadheads/night), platform, train_kind, direction, late_minutes:
<minutes>, is_alert}. The position -> (line, station) table was extracted
from the page's trainPosControll.js bundle (293 section entries, verified
17/17 against a live capture) and committed as data/keikyu-positions.json.

Train numbers are bare (1131) vs dokorail's suffixed (1131M?) - prefix
matching; night trains with train_no "0" fall through to the ID-less
position-time matcher (find_leg_by_position) via _station.
"""

import json

from . import common

BASE = "https://app-kq.net"
TRAIN_URL = BASE + "/api/train"
POSITIONS_PATH = "keikyu-positions.json"

DELAY_THRESHOLD_MIN = 1

# keikyu line_code -> (dokorail-style line title, suki line name, dokorail railways)
LINE_MAP = {
    1: ("京急本線", "京急本線", ["Keikyu.Main", "Keikyu.Kurihama"]),
    2: ("空港線", "京急空港線", ["Keikyu.Airport", "Keikyu.Main"]),
    3: ("久里浜線", "京急久里浜線", ["Keikyu.Kurihama"]),
    4: ("逗子線", "逗子線", ["Keikyu.Zushi"]),
    5: ("大師線", "京急大師線", ["Keikyu.Daishi"]),
}


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        try:
            trains = src.offline_json("kq-train.json")
        except Exception:
            return None
        try:
            positions = src.offline_json(POSITIONS_PATH)
        except Exception:
            positions = {}
    else:
        try:
            trains = common.get_json(TRAIN_URL)
        except Exception as exc:
            print(f"keikyu feed: {type(exc).__name__}: {str(exc)[:120]}")
            return None
        positions = None
        try:
            positions = common.get_json(common.SUKI_STATIC_SITE + "/" + POSITIONS_PATH)
        except Exception:
            positions = _bundled_positions()
    if not isinstance(trains, list):
        return None
    positions = positions or _bundled_positions()

    body = []
    for t in trains:
        if not isinstance(t, dict):
            continue
        late_min = int(t.get("late_minutes") or 0)
        if late_min < DELAY_THRESHOLD_MIN:
            continue
        pos = (positions or {}).get(str(t.get("position") or ""))
        if not pos:
            continue
        line_title, suki_line, railways = LINE_MAP.get(
            pos["line_code"], (f"keikyu:{pos['line_code']}", None, [])
        )
        number = str(t.get("train_no") or "").strip()
        entry = {
            "owl:sameAs": f"Keikyu-guide:{pos.get('line_code')}.{t.get('position')}.{number}",
            "odpt:railway": "odpt.Railway:Keikyu",
            "odpt:trainNumber": number if number and number != "0" else None,
            "odpt:delay": late_min * 60,
            "odpt:trainType": None,
            "_line_ja": line_title,
            "_suki_line": suki_line,
            "_station": pos.get("station"),
            "_direction": t.get("direction"),
        }
        if number and number != "0":
            entry["_number_prefix"] = number
        body.append(entry)
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "app-kq.net",
    }


def _bundled_positions():
    """The static table ships in the repo (data/keikyu-positions.json);
    fetch() normally reads it via the offline/committed path, so this is
    only a guard for live runs where the data dir is the source of truth."""
    from pathlib import Path

    p = Path(__file__).resolve().parent.parent / "data" / POSITIONS_PATH
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}
