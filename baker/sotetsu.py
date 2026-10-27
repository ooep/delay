"""Sotetsu (相鉄) live train feed via the native Ktor JSON API.

Endpoints (no key; reverse-engineered 2026-10-27):
    GET https://api.sotetsuapp.com/api/v2/lines       static master (works
        from this sandbox; snake_case fields, 12 lines incl. through
        operators 東急東横線/メトロ/東武東上線/埼京線...).
    GET https://api.sotetsuapp.com/api/v2/operations   live positions.

operations payload (per prior RE; field naming unknown - lines uses
snake_case, the RE notes say camelCase, so the parser accepts BOTH):
    {"operations":[{lineId, stationId, trains:{up:[TimeLine], down:[TimeLine]}}]}
    TimeLine{time, id(app-internal, NOT a train number), delay(分),
             kindNameJa, destinationLineId, destinationStationId}

Mapping (verified vs captured lines.json + lines.json in data/):
    lineId 1 -> 本線            -> odpt.Railway:Sotetsu.Main
    lineId 2 -> いずみ野線      -> odpt.Railway:Sotetsu.Izumino
    lineId 3 -> 新横浜線        -> odpt.Railway:Sotetsu.SotetsuShinYokohama
    lineId 100/311-324 (through operators) -> skipped (no dokorail railway
        in our lines.json; flagged in the test report).

NOTE: operations returned HTTP 400 (empty body, Cowboy) from this sandbox's
egress on 2026-10-27 despite /lines answering 200 - method/headers/query
probed exhaustively. Live path returns None until re-verified from the
Actions runner; offline fixtures replay the same parser. Conditional GET
(If-None-Match) is intentionally NOT used - plain GET keeps the runner simple.
"""

from . import common

LINES_URL = "https://api.sotetsuapp.com/api/v2/lines"
OPERATIONS_URL = "https://api.sotetsuapp.com/api/v2/operations"

DELAY_THRESHOLD_MIN = 1  # only emit delay >= 60 s

# lineId -> (dokorail railway id suffix, ja line name)
LINE_MAP = {
    1: ("Sotetsu.Main", "相鉄本線"),
    2: ("Sotetsu.Izumino", "いずみ野線"),
    3: ("Sotetsu.SotetsuShinYokohama", "相鉄新横浜線"),
}

# module-level cache: station id -> ja name (built from /lines; covers the
# through-service lines too, which is where destinationStationId points)
_STATION_MASTER = None


def _g(d, *keys, default=None):
    """Accept both camelCase (RE notes) and snake_case (observed on /lines)."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def _build_master(lines_payload):
    master = {}
    for line in (lines_payload or {}).get("lines") or []:
        for st in line.get("stations") or []:
            sid = st.get("id")
            if sid is not None:
                master[sid] = st.get("name_ja") or st.get("nameJa")
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
            payload = src.offline_json("sotetsu-operations.json")
        except Exception:
            return None
    else:
        try:
            payload = common.get_json(OPERATIONS_URL)
        except Exception as exc:
            print(f"sotetsu operations: {type(exc).__name__}: {str(exc)[:120]}")
            return None
    master = _load_master(src)

    body = []
    for op in (payload or {}).get("operations") or []:
        line_id = _g(op, "lineId", "line_id")
        railway_id, line_ja = LINE_MAP.get(line_id, (None, None))
        if not railway_id:
            continue  # through-operator rows (100/311-324) not mapped yet
        station_id = _g(op, "stationId", "station_id")
        trains = op.get("trains") or {}
        for direction, bucket in (("up", trains.get("up") or []),
                                  ("down", trains.get("down") or [])):
            for tl in bucket:
                if not isinstance(tl, dict):
                    continue
                try:
                    delay_min = int(_g(tl, "delay", default=0) or 0)
                except (TypeError, ValueError):
                    delay_min = 0
                if delay_min < DELAY_THRESHOLD_MIN:
                    continue
                dest_id = _g(tl, "destinationStationId", "destination_station_id")
                body.append(
                    {
                        "owl:sameAs": f"Sotetsu:{railway_id}.{direction}."
                        f"{dest_id}.{station_id}",
                        "odpt:railway": f"odpt.Railway:{railway_id}",
                        "odpt:trainNumber": None,  # TimeLine.id is app-internal
                        "odpt:delay": delay_min * 60,
                        "odpt:trainType": _g(tl, "kindNameJa", "kind_name_ja"),
                        "_line_ja": line_ja,
                        "_suki_line": line_ja,
                        "_station": master.get(station_id),
                        "_dest": master.get(dest_id),
                        "_direction": direction,
                    }
                )
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "api.sotetsuapp.com/api/v2/operations",
    }
