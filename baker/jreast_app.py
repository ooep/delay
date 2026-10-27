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
# train files mix lineCodes (e.g. train_57.json holds lineCode 58 runs;
# train_88.json holds 88/90/86), so per-train grouping uses the train's own
# lineCode. Suki pool names are chosen against data/suki/tmaps.json's pool;
# pools that don't exist there (京浜東北線, 埼京線...) map to the physical
# line the route actually runs on and silently fail to resolve rather than
# mis-tagging.
LINES = {
    # --- Shinkansen (already live) ---
    "88": {"board": "東北新幹線", "suki": "東北新幹線", "railway": "JR-East.TohokuShinkansen"},
    "87": {"board": "上越新幹線", "suki": "上越新幹線", "railway": "JR-East.JoetsuShinkansen"},
    "89": {"board": "北陸新幹線", "suki": "北陸新幹線", "railway": "JR-East.HokurikuShinkansen"},
    "90": {"board": "山形新幹線", "suki": "奥羽本線", "railway": "JR-East.YamagataShinkansen"},
    "86": {"board": "秋田新幹線", "suki": "田沢湖線", "railway": "JR-East.AkitaShinkansen"},
    # --- 首都圏 commuter ---
    "40": {"board": "京浜東北線", "suki": "根岸線", "railway": "JR-East.KeihinTohoku"},
    "41": {"board": "京葉線", "suki": "京葉線", "railway": "JR-East.Keiyo"},
    "43": {"board": "埼京線", "suki": "川越線", "railway": "JR-East.Saikyo"},
    "44": {"board": "相模線", "suki": "相模線", "railway": "JR-East.Sagami"},
    "46": {"board": "湘南新宿ライン", "suki": "東海道本線", "railway": "JR-East.Shonei"},
    "47": {"board": "常磐線快速", "suki": "常磐線", "railway": "JR-East.JobanRapid"},
    "49": {"board": "常磐線各停", "suki": "常磐線", "railway": "JR-East.JobanLocal"},
    "52": {"board": "総武快速線", "suki": "総武本線", "railway": "JR-East.SobuRapid"},
    "53": {"board": "総武本線", "suki": "総武本線", "railway": "JR-East.Sobu"},
    "54": {"board": "外房線", "suki": "外房線", "railway": "JR-East.Sotobo"},
    "55": {"board": "高崎線", "suki": "高崎線", "railway": "JR-East.Takasaki"},
    "56": {"board": "中央・総武各停", "suki": "中央線", "railway": "JR-East.ChuoLocal"},
    "57": {"board": "中央線快速", "suki": "中央本線", "railway": "JR-East.ChuoRapid"},
    "58": {"board": "中央本線", "suki": "中央本線", "railway": "JR-East.Chuo"},
    "60": {"board": "東海道線", "suki": "東海道本線", "railway": "JR-East.Tokaido"},
    "62": {"board": "成田線", "suki": "成田線", "railway": "JR-East.Narita"},
    "63": {"board": "南武線", "suki": "南武線", "railway": "JR-East.Nambu"},
    "67": {"board": "武蔵野線", "suki": "武蔵野線", "railway": "JR-East.Musashino"},
    "69": {"board": "横須賀線", "suki": "横須賀線", "railway": "JR-East.Yokosuka"},
    "70": {"board": "横浜線", "suki": "横浜線", "railway": "JR-East.Yokohama"},
    "101": {"board": "成田エクスプレス", "suki": "総武本線", "railway": "JR-East.NaritaExpress"},
    "102": {"board": "上野東京ライン", "suki": "東海道本線", "railway": "JR-East.UenoTokyo"},
    "31": {"board": "五日市線", "suki": "五日市線", "railway": "JR-East.Itsukaichi"},
    "32": {"board": "伊東線", "suki": "伊東線", "railway": "JR-East.Ito"},
    "33": {"board": "内房線", "suki": "内房線", "railway": "JR-East.Uchibo"},
    "34": {"board": "宇都宮線", "suki": "東北本線", "railway": "JR-East.Utsunomiya"},
    "35": {"board": "青梅線", "suki": "青梅線", "railway": "JR-East.Ome"},
    # --- 東北・信越 commuter ---
    "21": {"board": "東北本線", "suki": "東北本線", "railway": "JR-East.TohokuMain"},
    "14": {"board": "常磐線", "suki": "常磐線", "railway": "JR-East.Joban"},
    "16": {"board": "仙山線", "suki": "仙山線", "railway": "JR-East.Senzan"},
    "17": {"board": "仙石線", "suki": "仙石線", "railway": "JR-East.Senseki"},
    "5": {"board": "奥羽本線", "suki": "奥羽本線", "railway": "JR-East.Ou"},
    "6": {"board": "山形線", "suki": "奥羽本線", "railway": "JR-East.Yamagata"},
    "4": {"board": "羽越本線", "suki": "羽越本線", "railway": "JR-East.Uetsu"},
    "73": {"board": "羽越本線", "suki": "羽越本線", "railway": "JR-East.Uetsu"},
    "45": {"board": "上越線", "suki": "上越線", "railway": "JR-East.Joetsu"},
    "78": {"board": "上越線", "suki": "上越線", "railway": "JR-East.Joetsu"},
    "71": {"board": "両毛線", "suki": "両毛線", "railway": "JR-East.Ryomo"},
    "77": {"board": "信越本線", "suki": "信越本線", "railway": "JR-East.Shinetsu"},
    "79": {"board": "信越本線", "suki": "信越本線", "railway": "JR-East.Shinetsu"},
    "82": {"board": "白新線", "suki": "白新線", "railway": "JR-East.Hakushin"},
    # --- 特急形 (heading to suki pool of the physical line) ---
    "901": {"board": "踊り子", "suki": "伊東線", "railway": "JR-East.Odoriko"},
    "902": {"board": "踊り子", "suki": "伊東線", "railway": "JR-East.Odoriko"},
    "903": {"board": "あずさ", "suki": "中央本線", "railway": "JR-East.Azusa"},
    "904": {"board": "ひたち", "suki": "常磐線", "railway": "JR-East.Hitachi"},
    "905": {"board": "あかぎ", "suki": "高崎線", "railway": "JR-East.Akagi"},
}

DEFAULT_LINE = {"board": "JR-East", "suki": "東北本線", "railway": "JR-East.Unknown"}

# files actually pulled: every cloudrail-native line code from the app's
# train-location menu (2026-10 CSV inventory), minus 90/86 which ride in 88.
PULL_FILES = [
    "88", "87", "89",
    "21-5", "21-3", "21-4", "21-2", "21-1", "14", "16", "17",
    "5-1", "5-2", "6", "4",
    "40", "41", "43", "44", "45", "46-1", "46-2", "47", "49",
    "52", "53", "54", "55", "56", "57", "58", "60",
    "62-1", "62-2", "62-3", "63", "67-1", "67-2", "69", "70", "71",
    "77", "78", "79", "82",
    "101-1", "101-3", "102-8", "102-12", "102-16",
    "31", "32", "33", "34", "35",
    "73", "901", "902", "903", "904", "905",
]

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
            meta = LINES.get(str(org.get("lineCode") or ""), LINES.get(str(file_id), DEFAULT_LINE))
            nickname = (org.get("nickname") or {}).get("ja")
            dest = ((org.get("destination") or {}).get("fullName") or {}).get("ja")
            stops_raw = []
            _find_stations(t, stops_raw)
            remaining = []  # [{name, plan}] for live stops after the current point
            seen = set()
            for s in stops_raw:
                if s.get("pass"):
                    continue
                name = (s.get("stationName") or {}).get("ja")
                if not name:
                    continue
                # cloudrail nests both directions' timetable under
                # kiten/shuten Hensei; the recursive scan picks both up, so the
                # station sequence can loop (上野->東京->上野->東京). Keep the
                # first run only.
                if name in seen:
                    break
                seen.add(name)
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
