"""Nishitetsu (西日本鉄道) live train feed for the baker.

Source: the official NNR online train-position system (no key, no WAF
observed, IIS/10.0):
    https://busnavi-railway.nnr.co.jp/tenjin_omuta/tenjin-omuta.json   live trains
    https://busnavi-railway.nnr.co.jp/tenjin_omuta/stopStation.json    stop master
    endpoints were read out of common/js/script.js (jsonBaseUrl01/02)

The position JSON is a grid diagram: each train sits on a (locationRow,
locationCol) cell, and the row -> station mapping is baked into the page
HTML. That mapping was parsed once into data/nishitetsu-positions.json (a
static table, same pattern as data/keikyu-positions.json). stopStation.json
carries 35 per-service-type ordered stop tables (infoType like "1u000"),
which disambiguate which station of a shared diagram row belongs to which
line (the Dazaifu/Amagi branches overlay the main line's rows, e.g. row 34
holds both T12 都府楼前 and D02 太宰府).

Delay semantics = category 3: text "N分遅れ" on trainInfoObjects[].delayMinutes
("" = on time, skipped). The trainNumber is an internal hex id
(3032313730FFFF00), NOT the public service number, so number matching against
dokorail legs fails by design and the Hanshin/Kintetsu-style ID-less path in
delays.py keys on (_station + delay + dest hint) via find_leg_by_position
instead.

Line attribution by the stop table's code set: tables containing D codes are
太宰府線, A codes are 甘木線, else 天神大牟田線. 貝塚線 is not part of this
NNR system.
"""

import json
import re

from . import common

BASE = "https://busnavi-railway.nnr.co.jp/tenjin_omuta/"
TRAINS_URL = BASE + "tenjin-omuta.json"
STOPS_URL = BASE + "stopStation.json"  # stop master; a refresh.py slow-cache candidate
POSITIONS_PATH = "nishitetsu-positions.json"  # static row->station table, ships in data/

DELAY_THRESHOLD_MIN = 1
DELAY_RE = re.compile(r"^(\d+)分遅れ")

# stop-table code prefix -> (board/dokorail title, suki line name, dokorail railways).
# Branch through-services (Amagi/Dazaifu) run on the Tenjin-Omuta main line for
# most of their path before branching off, so the position engine must search
# BOTH the branch bundle and Tenjin-Omuta - find_leg_by_position picks the
# directed leg via dest_hint (dest==leg terminus), so no cross-fire.
LINE_MAP = {
    "D": ("西日本鉄道太宰府線", "太宰府線", ["Nishitetsu.Dazaifu", "Nishitetsu.TenjinOmuta"]),
    "A": ("西日本鉄道甘木線", "西鉄甘木線", ["Nishitetsu.Amagi", "Nishitetsu.TenjinOmuta"]),
    "T": ("西日本鉄道天神大牟田線", "天神大牟田線", ["Nishitetsu.TenjinOmuta"]),
}


def _get_json_bom(url):
    """The NNR JSONs start with a UTF-8 BOM; common.get_json's json.loads on
    bytes would raise, so decode with utf-8-sig first."""
    data = common.http_get(url)
    return json.loads(data.decode("utf-8-sig"))


def _bundled_positions():
    """The static row->station table ships in the repo (data/...). Mirrors
    keikyu._bundled_positions(); jr-static serve is a later deploy step."""
    from pathlib import Path

    p = Path(__file__).resolve().parent.parent / "data" / POSITIONS_PATH
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_positions():
    """row -> {station-code: name}; per-line sorted station rows."""
    positions = None
    try:
        positions = common.get_json(common.SUKI_STATIC + "/" + POSITIONS_PATH)
    except Exception:
        positions = _bundled_positions()
    positions = positions or _bundled_positions()
    if not positions:
        return None
    rows = {}
    for r, entries in (positions.get("rows") or {}).items():
        try:
            rows[int(r)] = {c: n for c, n in entries.items()}
        except (TypeError, ValueError):
            continue
    return rows


def _load_stop_tables(raw_stops):
    """infoType -> {station codes on that service pattern}."""
    tables = {}
    for entry in (raw_stops.get("stopStationInfo") or []):
        codes = {
            o.get("stationNumber")
            for o in (entry.get("stopStationInfoObjects") or [])
            if o.get("stationNumber")
        }
        if codes:
            tables[entry.get("infoType")] = codes
    return tables


def _line_for(info_type, tables):
    for prefix in ("D", "A", "T"):
        if any((c or "").startswith(prefix) for c in tables.get(info_type) or ()):
            return LINE_MAP[prefix]
    return LINE_MAP["T"]


def _current_station(row, info_type, tables, rows):
    """Exact station at the train's row that belongs to its service pattern;
    fall back to the nearest such station row (grid rows between stations are
    track segments; the position-time engine tolerates the +/- few minutes)."""
    codes = tables.get(info_type) or set()
    if not codes:
        return None
    for code, name in (rows.get(row) or {}).items():
        if code in codes:
            return name
    best = None
    for r, entries in rows.items():
        for code, name in entries.items():
            if code in codes:
                d = abs(r - row)
                if best is None or d < best[0]:
                    best = (d, name)
    return best[1] if best else None


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        raw = json.loads((src.offline.root / "nst-trains.json").read_text(encoding="utf-8-sig"))
        raw_stops = json.loads((src.offline.root / "nst-stops.json").read_text(encoding="utf-8-sig"))
    else:
        try:
            raw = _get_json_bom(TRAINS_URL) or {}
            raw_stops = _get_json_bom(STOPS_URL) or {}
        except Exception as exc:
            print(f"nishitetsu feed: {type(exc).__name__}: {str(exc)[:120]}")
            return None
    tables = _load_stop_tables(raw_stops)
    rows = _load_positions()
    body = []
    for loc in raw.get("locationObjects") or []:
        if not isinstance(loc, dict):
            continue
        row = None
        try:
            row = int(loc.get("locationRow"))
        except (TypeError, ValueError):
            row = None
        for t in loc.get("trainInfoObjects") or []:
            if not isinstance(t, dict):
                continue
            m = DELAY_RE.match(t.get("delayMinutes") or "")
            if not m:  # "" = on time
                continue
            delay_min = int(m.group(1))
            if delay_min < DELAY_THRESHOLD_MIN:
                continue
            info_type = t.get("stopStationInfoType")
            title, suki_line, railways = _line_for(info_type, tables)
            station = None
            if rows and row is not None:
                station = _current_station(row, info_type, tables, rows)
            number = str(t.get("trainNumber") or "")
            body.append(
                {
                    "owl:sameAs": f"Nishitetsu:{info_type}.{number}",
                    "odpt:railway": "odpt.Railway:Nishitetsu",
                    "odpt:trainNumber": number,
                    "odpt:delay": delay_min * 60,
                    # internal hex id, never matches dokorail numbers - the
                    # position-time engine (find_leg_by_position) must resolve
                    # the leg from (_station, delay, _dest) instead (Hanshin/
                    # Kintetsu ID-less precedent)
                    "_number_prefix": number,
                    "_railways": railways,
                    "_line_ja": title,
                    "_suki_line": suki_line,
                    "_station": station,
                    "_dest": t.get("destStationName"),
                    "_direction": "up" if loc.get("trainDirection") == "1" else "down",
                }
            )
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "source_time": raw.get("fileCreatedTime"),  # YYYYMMDDHHMMSS; _source_lag_minutes parses it
        "body": body,
        "source": "busnavi-railway.nnr.co.jp",
    }
