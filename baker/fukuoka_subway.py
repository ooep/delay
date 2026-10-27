"""Fukuoka City Subway (福岡市交通局 地下鉄) live feed.

Reverse-engineered 2026-10-27. The ちかまるナビ status page
(https://subway.city.fukuoka.lg.jp/status/) ships two keyless JSON feeds:

    GET https://subway.city.fukuoka.lg.jp/running.json        live train positions
    GET https://subway.city.fukuoka.lg.jp/suspension.json     line-level planned-suspension banner
        -> {"information": "..."}  (empty string when all clear)

`running.json` is a bare JSON array. At the reverse-engineering hour
(22:45 JST, 2026-10-27) it returned `[]` — no train records are exposed while
service is on-schedule. The populated per-train record shape was therefore
**not observed live**; the parser below is written to tolerate the common
odpt-like keys (delay / railway / trainNumber / station) but will emit nothing
until a populated sample is captured in daytime service. We do NOT guess the
schema.

Delay policy: only emit records that carry an explicit delay >= 60s. A bare
position with no delay number is dropped (宁漏不误报).
"""

from . import common

RUNNING_URL = "https://subway.city.fukuoka.lg.jp/running.json"
SUSP_URL = "https://subway.city.fukuoka.lg.jp/suspension.json"

# railway codes for the three Fukuoka subway lines.
RAILWAY_BY_LINE = {
    "空港線": "odpt.Railway:FukuokaSubway.Kuko",
    "箱崎線": "odpt.Railway:FukuokaSubway.Hakozaki",
    "七隈線": "odpt.Railway:FukuokaSubway.Nanakuma",
}


def _delay_seconds(rec):
    """Pull a delay in seconds from whatever key the populated record uses."""
    for key in ("delay", "odpt:delay", "delaySec", "delay_sec", "delay_min"):
        v = rec.get(key)
        if v is None:
            continue
        try:
            sec = int(v)
        except (TypeError, ValueError):
            continue
        # delay_min is whole minutes; seconds are seconds.
        if key == "delay_min":
            sec *= 60
        return sec
    return None


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        try:
            raw = src.offline_json("fk-running.json")
        except Exception:
            raw = []
        try:
            susp = src.offline_json("fk-suspension.json")
        except Exception:
            susp = {}
    else:
        try:
            raw = common.get_json(RUNNING_URL)
        except Exception as exc:
            print(f"fukuoka running.json: {type(exc).__name__}: {str(exc)[:120]}")
            raw = []
        try:
            susp = common.get_json(SUSP_URL) or {}
        except Exception:
            susp = {}

    body = []
    for rec in raw or []:
        if not isinstance(rec, dict):
            continue
        sec = _delay_seconds(rec)
        if sec is None or sec < 60:
            continue
        line_ja = rec.get("line") or rec.get("lineName") or rec.get("railway")
        body.append({
            "odpt:delay": sec,
            "odpt:railway": RAILWAY_BY_LINE.get(line_ja, "odpt.Railway:FukuokaSubway"),
            "odpt:trainNumber": rec.get("trainNumber") or rec.get("no"),
            "odpt:trainType": rec.get("trainType"),
            "_line_ja": line_ja,
            "_suki_line": line_ja,
            "_station": rec.get("station") or rec.get("stationName"),
            "_dest": rec.get("destination") or rec.get("destinationStationName"),
            "_direction": rec.get("direction"),
        })

    info = (susp or {}).get("information") if isinstance(susp, dict) else None
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "subway.city.fukuoka.lg.jp/running.json",
        "_suspension": info or "",
        "_note": ("running.json empty at off-peak/night; populated per-train "
                  "record shape unobserved -> defensive parser, no fabricated rows"),
    }
