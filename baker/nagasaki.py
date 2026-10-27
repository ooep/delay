"""Nagasaki Electric Tramway (長崎電気軌道) live feed.

Reverse-engineered 2026-10-27. The site https://www.naga-den-spmap.com is a
React SPA whose axios client uses baseURL `https://www.naga-den-spmap.com/api`
and three keyless GET endpoints:

    GET /api/message       {"alert_level":1,"message":"平常どおり運行しています。",
                            "popup_flg":false,"not_operation_flg":false}
    GET /api/master         {"stations":[{station_no,latitude,longtitude,...} x39]}
    GET /api/location_data  {"datas":{...live tram positions...},"time":ISO8601}

Observed 22:45 JST 2026-10-27: location_data == {"datas":{},"time":"..."}
(no trams reporting after last service), message == "on schedule". The
populated `datas` shape (tram id -> lat/lng/line/direction) was not captured
live; the parser below is tolerant but emits only rows that carry an explicit
delay >= 60s. This source has no per-train delay field, so body is empty.
"""

from . import common

BASE = "https://www.naga-den-spmap.com"
LOC_URL = BASE + "/api/location_data"
MSG_URL = BASE + "/api/message"
MASTER_URL = BASE + "/api/master"


def _get_json(url, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            return common.get_json(url, headers={
                "Accept": "application/json",
                "Referer": BASE + "/",
            }, retries=1)
        except Exception as exc:
            last = exc
            if attempt < retries:
                import time
                time.sleep(1.5)
    raise last


def _delay_seconds(rec):
    for key in ("delay", "delay_sec", "delaySec", "delayMin", "delay_min"):
        v = rec.get(key)
        if v is None:
            continue
        try:
            sec = int(v)
        except (TypeError, ValueError):
            continue
        return sec * 60 if key.endswith("Min") else sec
    return None


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        loc = src.offline_json("ng-location.json")
        try:
            msg = src.offline_json("ng-message.json")
        except Exception:
            msg = {}
    else:
        try:
            loc = _get_json(LOC_URL)
        except Exception as exc:
            print(f"nagasaki location_data: {type(exc).__name__}: {str(exc)[:120]}")
            loc = {"datas": {}}
        try:
            msg = _get_json(MSG_URL)
        except Exception:
            msg = {}

    datas = (loc or {}).get("datas") or {}
    body = []
    for tid, rec in datas.items():
        if not isinstance(rec, dict):
            continue
        sec = _delay_seconds(rec)
        if sec is None or sec < 60:
            continue
        body.append({
            "odpt:delay": sec,
            "odpt:railway": "odpt.Railway:Nagasaki.Nagasaki",
            "odpt:trainNumber": rec.get("trainNumber") or str(tid),
            "odpt:trainType": rec.get("trainType"),
            "_line_ja": "長崎電軌道線",
            "_suki_line": "長崎電軌道線",
            "_station": rec.get("station"),
            "_dest": rec.get("destination"),
            "_direction": rec.get("direction"),
        })

    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "source_time": (loc or {}).get("time"),
        "body": body,
        "source": "naga-den-spmap.com/api/location_data",
        "_message": (msg or {}).get("message") if isinstance(msg, dict) else None,
        "_trams_reporting": len(datas),
        "_note": ("live positions in datas{}; no per-train delay published -> no body rows"),
    }
