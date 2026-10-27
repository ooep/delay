"""JR-East official app (cloudrail) per-train delay for Shinkansen.

Reverse-engineered 2026-10 from the JR East app (jp.co.jreast, base.apk).
The app's live-position backend:

    POST /com/api/userinfo/register   {"osType":"android"} -> {"userId": "..."}
        anonymous registration; the Azure Front Door WAF requires this
        User-ID header (no valid User-ID -> "The request is blocked" for
        every client, browser included; it is NOT an IP block).
    GET  azurefd/api/getresource?name=jrelines/version/version.json
        -> transaction.current.url = "jrelines/transaction/2.0.0/"
    GET  azurefd/api/getresource?name={tx}train_<lineCode>.json
        -> live trains: trainNo, delayMinute.value, remaining stops with
           plan*/prospect* arrival/departure (prospect = delay-adjusted).

Line codes (cloudrail): 88 東北・北海道 / 87 上越 / 89 北陸 / 90 山形 /
86 秋田. Each train record carries nickname (はやぶさ/やまびこ/...),
destination, series (E5系/...), car count and the remaining-stop timetable.

Board integration is suki-direct (like jrcentral's Tokaido shinkansen):
suki has no dokorail timetable bundle for shinkansen lines, so we report
_station (first remaining stop) + _dest (terminal) and let delays.py's
resolve_direct_by_time pin the exact suki key.
"""

import json
import time
import urllib.parse
import urllib.request

from . import common

BASE = "https://jreapp-apgw.cloudrail.jp/com/api"
FD = "https://FrontDoorEndPoint01-fheefececgbugmbf.z01.azurefd.net/api"
# urllib cannot send non-latin-1 header bytes; the upstream accepts the
# percent-encoded UA form equally (verified 2026-10-27).
UA = ("JR%E6%9D%B1%E6%97%A5%E6%9C%AC%E3%82%A2%E3%83%97%E3%83%AA; "
      "Dalvik/2.1.0 (Linux; U; Android 14; Pixel 8 Build/UP1A.231005.007)")

# cloudrail lineCode -> (board line name, suki line pool, railway id).
# train_88.json actually mixes lineCode 88/90/86 (混编 running on the same
# Tohoku corridor), so per-train grouping uses the train's own lineCode, not
# the file's. Only 88/87/89 files exist as separate pulls; 90/86 rides in 88.
LINES = {
    "88": {"board": "東北新幹線", "suki": "東北新幹線", "railway": "JR-East.TohokuShinkansen"},
    "87": {"board": "上越新幹線", "suki": "上越新幹線", "railway": "JR-East.JoetsuShinkansen"},
    "89": {"board": "北陸新幹線", "suki": "北陸新幹線", "railway": "JR-East.HokurikuShinkansen"},
    # mini-Shinkansen: suki tags つばさ/こまち runs on the physical lines
    "90": {"board": "山形新幹線", "suki": "奥羽本線", "railway": "JR-East.YamagataShinkansen"},
    "86": {"board": "秋田新幹線", "suki": "田沢湖線", "railway": "JR-East.AkitaShinkansen"},
}

# files actually pulled (the others come mixed into train_88.json)
PULL_FILES = ["88", "87", "89"]

DELAY_THRESHOLD_MIN = 1
_UID = None  # process-live; each GitHub-Actions run registers fresh (fast)


def _headers(uid=""):
    h = {
        "User-Agent": UA,
        "Platform": "android",
        "Application-Identifier": "jp.co.jreast",
        "Client-Version": "1040",
        "X-Yamachan-Version": "19",
        "Accept": "application/json",
    }
    if uid:
        h["User-ID"] = uid
    return h


def _register(src):
    global _UID
    if src.offline:
        res = src.offline_json("jr-app-register.json")
        _UID = res["userId"]
        return _UID
    req = urllib.request.Request(
        BASE + "/userinfo/register",
        data=b'{"osType":"android"}',
        headers={**_headers(), "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        res = json.loads(resp.read())
    _UID = res["userId"]
    return _UID


def _get(src, path):
    """getresource with WAF retry: on block, re-register once and retry."""
    global _UID
    uid = _UID or _register(src)
    if src.offline:
        name = path.rsplit("/", 1)[-1]
        return src.offline_json("jr-app-" + name)
    url = f"{FD}/getresource?name={common.urllib.parse.quote(path)}"
    try:
        data = common.get_json(url, headers=_headers(uid))
    except Exception as exc:
        # 403 "The request is blocked" -> stale User-ID, re-register once
        if "403" in str(exc) or "463" in str(exc):
            _UID = None
            _register(src)
            data = common.get_json(url, headers=_headers(_UID))
        else:
            raise
    return data


def _walk_trains(obj, out):
    if isinstance(obj, list):
        for v in obj:
            _walk_trains(v, out)
    elif isinstance(obj, dict):
        if isinstance(obj.get("trainNo"), str) and obj.get("delayMinute"):
            out.append(obj)
        for v in obj.values():
            _walk_trains(v, out)


def _find_stations(obj, out):
    if isinstance(obj, list):
        for v in obj:
            _find_stations(v, out)
    elif isinstance(obj, dict):
        if obj.get("stationName") and (obj.get("planArrival") or obj.get("planDeparture")):
            out.append(obj)
            return
        for v in obj.values():
            _find_stations(v, out)


def _hm(s):
    return s or ""


def _hhmm_min(s):
    """'10:28' -> 628; None/'' -> None."""
    if not s:
        return None
    try:
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def fetch(src=None):
    src = src or common.Source()
    now = common.jst_now()
    source_time = None
    body = []
    try:
        _register(src)
        if src.offline:
            ver = src.offline_json("jr-app-version.json")
        else:
            ver = common.get_json(
                f"{FD}/getresource?name=jrelines/version/version.json",
                headers=_headers(_UID),
            )
        tx_url = ver["transaction"]["current"]["url"]
    except Exception as exc:
        print(f"jreast-app setup: {type(exc).__name__}: {str(exc)[:140]}")
        return {"ok": False, "fetchedAt": now.isoformat(), "body": [], "source": "JR-East-app"}

    for file_id in PULL_FILES:
        try:
            doc = _get(src, f"{tx_url}train_{file_id}.json")
        except Exception as exc:
            print(f"jreast-app {file_id}: {type(exc).__name__}: {str(exc)[:140]}")
            continue
        if doc.get("dateTime"):
            source_time = doc["dateTime"]
        trains = []
        _walk_trains(doc, trains)
        for t in trains:
            dm = t.get("delayMinute") or {}
            if not dm.get("valid") or (dm.get("value") or 0) < DELAY_THRESHOLD_MIN:
                continue
            org = (t.get("organizationBasicInfo") or [{}])[0]
            # per-train lineCode (88 file mixes 88/90/86)
            meta = LINES.get(str(org.get("lineCode") or file_id), LINES["88"])
            nickname = (org.get("nickname") or {}).get("ja")
            dest = ((org.get("destination") or {}).get("fullName") or {}).get("ja")
            stops_raw = []
            _find_stations(t, stops_raw)
            remaining = []  # [{name, plan}] for live stops after the current point
            for s in stops_raw:
                if s.get("pass"):
                    continue
                name = (s.get("stationName") or {}).get("ja")
                if not name:
                    continue
                remaining.append({
                    "name": name,
                    "plan": _hm(s.get("planArrival")) or _hm(s.get("planDeparture")),
                })
            first = remaining[0]["name"] if remaining else None
            # feed-provided official timetable for resolve_direct_by_time:
            # exact scheduled passage at the reported stop + the remaining
            # stop sequence (shinkansen has no dokorail timetable bundle)
            official = None
            if remaining:
                dep_min = _hhmm_min(remaining[0]["plan"])
                if dep_min is not None:
                    official = {
                        "departure": dep_min,
                        "stations": [r["name"] for r in remaining[1:]],
                        "terminal": dest,
                    }
            body.append({
                "owl:sameAs": f"JR-East-App:{org.get('lineCode') or file_id}.{t['trainNo']}",
                "odpt:railway": f"odpt.Railway:{meta['railway']}",
                "odpt:trainNumber": t["trainNo"],
                "odpt:delay": int(dm["value"]) * 60,
                "odpt:trainType": nickname,
                "odpt:carComposition": (t.get("cars") or {}).get("value"),
                "_line_ja": meta["board"],
                "_suki_line": meta["suki"],
                "_station": first,
                "_upcoming": [r["name"] for r in remaining],
                "_dest": dest,
                "_nickname": nickname,
                "_official": official,
            })
        time.sleep(0.2)  # be polite to the WAF
    return {
        "ok": True,
        "fetchedAt": now.isoformat(),
        "source_time": source_time,
        "body": body,
        "source": "JR-East-app(cloudrail)",
    }
