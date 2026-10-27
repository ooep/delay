"""Sotetsu (相鉄) live train feed.

REVISED 2026-10-27: the native Ktor endpoint
    GET https://api.sotetsuapp.com/api/v2/operations   (also v1)
returns HTTP 400 with empty body from BOTH this sandbox and the GH Actions
runner - it is gated on a server-registered popinfo device id that the APK
base split (iridge popinfo SDK) does not carry. The web frontend
(www.sotetsuapp.com/lines/main/trains) uses a different, open data host:

    GET https://external-data.sotetsuapp.com/tid/trains.json     (live, 200)

trains.json record:
    {"station_id":17, "next_station_id":11, "position":0|1,
     "direction":"up"|"down", "train_kind_id":7, "delay":1,
     "train_number":"2083", "operation_number":59,
     "destination_station_id":19, "line_id":1}

station/destination names come from the static v1 master:
    GET https://api.sotetsuapp.com/api/v1/lines   (200, 12 lines incl.
    through operators; stations[].id -> name_ja global map)

Mapping:
    lineId 1 -> 本線            -> odpt.Railway:Sotetsu.Main
    lineId 2 -> いずみ野線      -> odpt.Railway:Sotetsu.Izumino
    lineId 3 -> 新横浜線        -> odpt.Railway:Sotetsu.SotetsuShinYokohama
    through-operator line_ids (東急/メトロ/東武/西武/埼玉/都営 JR) skipped
    (no dokorail railway in our lines.json; covered by tokyu.py / JR feeds).
"""

from . import common

TID_URL = "https://external-data.sotetsuapp.com/tid/trains.json"
LINES_URL = "https://api.sotetsuapp.com/api/v1/lines"

DELAY_THRESHOLD_MIN = 1  # only emit delay >= 60 s

# lineId -> (dokorail railway id suffix, ja line name)
LINE_MAP = {
    1: ("Sotetsu.Main", "相鉄本線"),
    2: ("Sotetsu.Izumino", "いずみ野線"),
    3: ("Sotetsu.SotetsuShinYokohama", "相鉄新横浜線"),
}

# module-level cache: station id -> ja name (built from v1/lines)
_STATION_MASTER = None


def _build_master(lines_payload):
    master = {}
    for line in (lines_payload or {}).get("lines") or []:
        for st in line.get("stations") or []:
            sid = st.get("id")
            if sid is not None:
                master[sid] = st.get("name_ja")
    return master


def _load_master(src):
    global _STATION_MASTER
    if _STATION_MASTER is not None:
        return _STATION_MASTER
    if src.offline:
        try:
            payload = src.offline_json("sotetsu-lines.json")
        except Exception:
            payload = {}
    else:
        try:
            payload = common.get_json(LINES_URL)
        except Exception as exc:
            print(f"sotetsu lines: {type(exc).__name__}: {str(exc)[:120]}")
            payload = {}
    _STATION_MASTER = _build_master(payload)
    return _STATION_MASTER


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        try:
            payload = src.offline_json("sotetsu-trains.json")
        except Exception:
            return None
    else:
        try:
            payload = common.get_json(TID_URL)
        except Exception as exc:
            print(f"sotetsu tid: {type(exc).__name__}: {str(exc)[:120]}")
            return None
    master = _load_master(src)

    body = []
    for tr in (payload or {}).get("trains") or []:
        if not isinstance(tr, dict):
            continue
        line_id = tr.get("line_id")
        railway_id, line_ja = LINE_MAP.get(line_id, (None, None))
        if not railway_id:
            continue  # through-operator rows; covered by other feeds
        try:
            delay_min = int(tr.get("delay") or 0)
        except (TypeError, ValueError):
            delay_min = 0
        if delay_min < DELAY_THRESHOLD_MIN:
            continue
        direction = tr.get("direction") == "up" and "上り" or "下り"
        station_id = tr.get("station_id")
        next_id = tr.get("next_station_id")
        # 当前位置：station_id 为当前站；若为 null 且 next_station_id 有值，
        # 说明车已离开上一站、正在站间（用下一站作为报告点）
        report_station = station_id if station_id is not None else next_id
        number = str(tr.get("train_number") or "").strip()
        dest_id = tr.get("destination_station_id")
        body.append({
            "owl:sameAs": f"Sotetsu:{railway_id}.{number or next_id}.{report_station}",
            "odpt:railway": f"odpt.Railway:{railway_id}",
            "odpt:trainNumber": number or None,
            "odpt:delay": delay_min * 60,
            "_line_ja": line_ja,
            "_suki_line": line_ja,
            "_station": master.get(report_station),
            "_dest": master.get(dest_id),
            "_direction": direction,
            "_number_prefix": number or None,
        })
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "external-data.sotetsuapp.com/tid/trains.json",
    }
