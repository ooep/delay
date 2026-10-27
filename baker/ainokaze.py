"""Ainokaze Toyama Railway (あいの風とやま鉄道) live feed for the baker.

Reverse-engineered 2026-10-28 from the あいトレ Angular app
(https://trafficinfo.ainokaze.co.jp/). The app's `api` base is "../api/"
which the browser resolves to "/api/", so the JSON endpoints live at:

    GET https://trafficinfo.ainokaze.co.jp/api/json/train.json?t=<ms>
    GET https://trafficinfo.ainokaze.co.jp/api/json/station.json
    GET https://trafficinfo.ainokaze.co.jp/api/json/delay.json
    GET https://trafficinfo.ainokaze.co.jp/api/json/trainTimetable/<num>.json

Every response is wrapped: {"result":{"status":"success","data":"<json>"}}
where `data` is a *string* that must be JSON.parsed a second time.

Live train records are ODPT-shaped:
    odpt:trainNumber  e.g. "1234M"
    odpt:delay        raw delay in SECONDS
    odpt:trainType     odpt.TrainType:AinokazeToyama.Local
    odpt:fromStation / odpt:toStation  station json_name strings
                       (e.g. "AinokazeToyama.AinokazeToyama.Toyama")
    odpt:railDirection display string, e.g. "糸魚川方面行き"
    odpt:startingStation / odpt:terminalStation station json_name

station.json maps json_name -> station_name_ja (the master also carries the
IR-Ishikawa through-section stations 金沢…倶利伽羅 and ETR/JR neighbours,
which the app shows for context). delay.json is only the display rounding
table (raw seconds -> rounded display minutes via GetDelay); the board wants
the raw seconds, so we emit odpt:delay untouched and keep the threshold at
>= 60 s.

Service hours are 05:00–01:00 JST; outside them train.json is "[]" and
config.json telop says サービス時間外 — an empty body then is the honest
answer, not a parser failure.
"""

import json
import time
import urllib.request

from . import common

API = "https://trafficinfo.ainokaze.co.jp/api/json/"
TRAIN_URL = API + "train.json?t={}"
STATION_URL = API + "station.json"

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

DELAY_THRESHOLD_SEC = 60


def _get_json(url, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Referer": "https://trafficinfo.ainokaze.co.jp/",
            })
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as exc:
            last = exc
            if attempt < retries:
                time.sleep(1.5)
    raise last


def _unwrap(payload):
    """{result:{status, data:"<json>"}} -> parsed data (or [])."""
    if not isinstance(payload, dict):
        return []
    res = payload.get("result") or {}
    if res.get("status") != "success":
        return []
    data = res.get("data")
    if data is None:
        return []
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return []
    return data or []


def _direction(rail_dir, terminal_ja):
    """上り = 金沢方面, 下り = 糸魚川・泊方面."""
    text = " ".join(x for x in (rail_dir, terminal_ja) if x)
    if not text:
        return None
    if any(k in text for k in ("金沢",)):
        return "上り"
    if any(k in text for k in ("糸魚川", "泊", "直江津")):
        return "下り"
    return None


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        trains = _unwrap(src.offline_json("ainok-train.json"))
        master = _unwrap(src.offline_json("ainok-station.json"))
    else:
        try:
            trains = _unwrap(_get_json(TRAIN_URL.format(int(time.time() * 1000))))
            master = _unwrap(_get_json(STATION_URL))
        except Exception as exc:
            print(f"ainokaze: {type(exc).__name__}: {str(exc)[:120]}")
            return {"ok": False, "fetchedAt": common.jst_now().isoformat(),
                    "body": [], "source": "trafficinfo.ainokaze.co.jp"}

    name_ja = {}
    for st in master:
        if isinstance(st, dict) and st.get("json_name"):
            name_ja[st["json_name"]] = st.get("station_name_ja")

    body = []
    for tr in trains:
        if not isinstance(tr, dict):
            continue
        delay = int(tr.get("odpt:delay") or 0)
        if delay < DELAY_THRESHOLD_SEC:
            continue
        number = tr.get("odpt:trainNumber")
        if not number:
            continue
        from_json = tr.get("odpt:fromStation")
        to_json = tr.get("odpt:toStation")
        term_json = tr.get("odpt:terminalStation")
        body.append({
            "owl:sameAs": f"Ainokaze:{number}",
            "odpt:railway": "odpt.Railway:AinokazeToyama.AinokazeToyama",
            "odpt:trainNumber": str(number),
            "odpt:delay": delay,
            "odpt:trainType": tr.get("odpt:trainType"),
            "_line_ja": "あいの風とやま鉄道線",
            "_suki_line": "あいの風とやま鉄道線",
            "_station": name_ja.get(from_json) if from_json else None,
            "_dest": name_ja.get(term_json) if term_json else (
                name_ja.get(to_json) if to_json else None),
            "_direction": _direction(tr.get("odpt:railDirection"),
                                      name_ja.get(term_json or "")),
        })
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "trafficinfo.ainokaze.co.jp",
    }
