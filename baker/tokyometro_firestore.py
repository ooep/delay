"""Tokyo Metro per-train feed via the App's private Cloud Firestore.

Reverse-engineered from 東京メトロmy!アプリ v7.2.0 (blutter on libapp.so):
`TrainDao._subscribeToLineById` -> Firestore collection "tokyometro",
doc per line (line api_name: ginza/marunouchi/...), field trainList is a
JSON-encoded string array of per-train objects.

Endpoint (anonymous, no ODPT_KEY):
  POST https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=<APIKEY>
       {"returnSecureToken":true} -> idToken (834 chars, ~1h expiry)
  GET  https://firestore.googleapis.com/v1/projects/mapp-91687/databases/(default)/documents/tokyometro/{lineId}
       Authorization: Bearer <idToken>

trainList item fields:
  trainNumber (A2155 / 1920N), delayTime (SECONDS), direction (1|2),
  startStation / endStation / pastStation / nextStation (station slugs,
  "tokyometro.ueno" or bare "nishimagome"), speedClass
  (Local/Express/LimitedExpress/RapidLimitedExpress/AirportRapidLimitedExpress),
  trainPosition (int), trainPositionFlag (B|S), trainCompany
  (TokyoMetro|Toei), updatedAt (ISO JST).

Only TokyoMetro 9 lines are emitted here: Toei's 4 lines already flow
through baker/odpt.py (keyless public mirror), so emitting them again
would double-count. Line-level status text keeps coming from
tokyometro_app.py; this feed adds the missing per-train layer.
"""

import json
import os
import time

from . import common

API_KEY = "AIzaSyB_uHi1BV4pyknzJ_QGk1XrsaiLa4e12IQ"
SIGNUP_URL = f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={API_KEY}"
FIRESTORE = ("https://firestore.googleapis.com/v1/projects/mapp-91687/"
             "databases/(default)/documents/tokyometro")

# lineId (Firestore doc id = app api_name) -> lines.json railway id
_LINE_RAILWAY = {
    "ginza": "TokyoMetro.Ginza",
    "marunouchi": "TokyoMetro.Marunouchi",
    "hibiya": "TokyoMetro.Hibiya",
    "tozai": "TokyoMetro.Tozai",
    "chiyoda": "TokyoMetro.Chiyoda",
    "yurakucho": "TokyoMetro.Yurakucho",
    "hanzomon": "TokyoMetro.Hanzomon",
    "namboku": "TokyoMetro.Namboku",
    "fukutoshin": "TokyoMetro.Fukutoshin",
}
_LINE_IDS = list(_LINE_RAILWAY)

# lineId -> ja line title (site display + suki line pool)
_LINE_JA = {
    "ginza": "銀座線",
    "marunouchi": "丸ノ内線",
    "hibiya": "日比谷線",
    "tozai": "東西線",
    "chiyoda": "千代田線",
    "yurakucho": "有楽町線",
    "hanzomon": "半蔵門線",
    "namboku": "南北線",
    "fukutoshin": "副都心線",
}

# speedClass -> ja (site display + suki type compatibility; unknown -> keep raw)
_TYPE_JA = {
    "Local": "普通",
    "Express": "急行",
    "LimitedExpress": "特急",
    "RapidLimitedExpress": "快速特急",
    "AirportRapidLimitedExpress": "エアポート快特",
}

_DELAY_THRESHOLD_SECONDS = 60  # same floor as baker/delays.py
_TOKEN = None
_TOKEN_AT = 0.0
_TOKEN_TTL = 50 * 60  # idToken expires ~1h; refresh comfortably before

from .tm_stations import STATION_JA  # noqa: E402


def _station_ja(slug):
    """Station slug (tokyometro.ueno / bare nishimagome) -> ja name."""
    if not slug:
        return None
    return STATION_JA.get(slug) or STATION_JA.get("tokyometro." + slug) or slug


def _token():
    """Fresh anonymous idToken, cached per process."""
    global _TOKEN, _TOKEN_AT  # noqa: PLW0603
    if _TOKEN and time.monotonic() - _TOKEN_AT < _TOKEN_TTL:
        return _TOKEN
    try:
        body = common.get_json(
            SIGNUP_URL,
            headers={"Content-Type": "application/json"},
            data=b'{"returnSecureToken":true}',
        )
        t = body.get("idToken")
        if t:
            _TOKEN, _TOKEN_AT = t, time.monotonic()
            return t
    except Exception as exc:
        print(f"tokyometro firestore signup: {type(exc).__name__}: {str(exc)[:120]}")
    return None


def _load_docs(src):
    """Return {lineId: [train,...]} for the 9 TokyoMetro lines."""
    if src.offline:
        try:
            return src.offline.get_json("tokyometro-trains.json")
        except Exception:
            return None
    tok = _token()
    if not tok:
        return None
    out = {}
    for lid in _LINE_IDS:
        try:
            doc = common.get_json(
                f"{FIRESTORE}/{lid}", headers={"Authorization": f"Bearer {tok}"}
            )
        except Exception as exc:
            print(f"tokyometro firestore {lid}: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        tl = doc.get("fields", {}).get("trainList", {}).get("stringValue")
        if not tl:
            continue
        try:
            out[lid] = json.loads(tl)
        except json.JSONDecodeError:
            continue
    return out


def fetch(src=None):
    src = src or common.Source()
    docs = _load_docs(src)
    if not docs:
        return None

    body = []
    seen = {}
    for lid, trains in docs.items():
        railway = _LINE_RAILWAY.get(lid)
        if not railway or not isinstance(trains, list):
            continue
        for t in trains:
            if not isinstance(t, dict):
                continue
            delay = t.get("delayTime")
            number = t.get("trainNumber")
            if not number or not isinstance(delay, int) or delay < _DELAY_THRESHOLD_SECONDS:
                continue
            # through-service duplicates: the same physical train appears in
            # two line docs (e.g. A2047S in both fukutoshin & yurakucho);
            # keep one entry per train number, the later/heavier report
            prev = seen.get(number)
            if prev and prev["odpt:delay"] >= delay:
                continue
            seen[number] = {
                "owl:sameAs": f"tokyometro:{lid}.{number}",
                "odpt:railway": railway,
                "odpt:trainNumber": str(number),
                "odpt:delay": delay,
                "odpt:trainType": _TYPE_JA.get(t.get("speedClass"), t.get("speedClass")),
                "odpt:fromStation": _station_ja(t.get("pastStation")),
                "odpt:toStation": _station_ja(t.get("endStation")),
                "_station": _station_ja(t.get("pastStation")),
                "_line_ja": _LINE_JA.get(lid) or railway,
                "_dest": _station_ja(t.get("endStation")),
                "_direction": t.get("direction"),
                "_pos": t.get("trainPosition"),
                "_pos_flag": t.get("trainPositionFlag"),
            }
    body = list(seen.values())
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "tokyometro app firestore (anonymous)",
    }
