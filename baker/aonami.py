"""Aonami Line (名古屋臨海高速鉄道 あおなみ線) live train feed.

Reverse-engineered 2026-10-30 from the あおなみ線 列車走行位置 map
(https://trainlocation-aonamiline.jp/). The page is a Nuxt SPA, so the HTML
carries no train data at all — the map is fed by:

    GET /data/traffic_info.json?<ms>
        {"UP":[{"dt":"2026/10/30 12:05:33","st":"0","cl":"0"}],
         "TS":[{"id":"E005D","tr":[{"no":"0359H","sy":"1","ik":"14",
                                    "dl":"0","hk":"1"}]}],
         "EK":[{"id":"D013","tr":[...]}, ...]}
    GET /config/station.json    {"station":[{"code":"2","num":"AN01","ja":"名古屋"}, ...]}
    GET /config/car_type.json   {"type":[{"code":"1","ja":"普通"}, ...]}

Slot ids name the train's position marker: "E###D"/"E###U" (TS) = standing at
station ###, "D###"/"U###" (EK) = on the segment approaching station ### —
verified by polling 0361H across one run: E003D → D005 → E005D. D = 下り
(toward 金城ふ頭), U = 上り (toward 名古屋). `dl` is the delay in MINUTES
(the map renders ＋N for 3..99), `ik` the destination station code and `sy`
the car type. 笹島 / 潮凪信号場 / 潮凪車庫 carry no station `num` (not in
passenger service), so ids that do not resolve to a numbered station are
skipped.

The host only accepts connections from Japanese IPs (every non-JP egress we
tested times out), which is why AONAMI_BASE points the baker at our own
jr-delay Worker proxy (https://jr-delay.suki.ing/jp/aonami, Smart Placement
near the origin). Unset -> hits the origin directly (local runs).
"""

import json
import os
import re
import time

from . import common

BASE = os.environ.get("AONAMI_BASE", "https://trainlocation-aonamiline.jp").rstrip("/")
TRAFFIC_URL = BASE + "/data/traffic_info.json?{}"
STATION_URL = BASE + "/config/station.json"
CARTYPE_URL = BASE + "/config/car_type.json"

DELAY_THRESHOLD_SEC = 60

RE_AT = re.compile(r"^E(\d{3})([DU])$")    # train at station ###
RE_EDGE = re.compile(r"^([DU])(\d{3})$")   # on the segment approaching ###

_cfg = {}


def _get_json(url, retries=3):
    return json.loads(common.http_get(url, retries=retries, timeout=25).decode("utf-8", "replace"))


def _config(src):
    if src.offline:
        return (src.offline.get_json("aonami-station.json"),
                src.offline.get_json("aonami-cartype.json"))
    if "station" not in _cfg or "type" not in _cfg:
        station = _get_json(STATION_URL)
        cartype = _get_json(CARTYPE_URL)
        _cfg["station"], _cfg["type"] = station, cartype
    return _cfg["station"], _cfg["type"]


def _stations(payload):
    out = {}
    for s in payload.get("station") or []:
        code = str(s.get("code") or "").strip()
        name = s.get("ja")
        if code and name and s.get("num"):
            out[code] = name
    return out


def _types(payload):
    return {
        str(t.get("code") or "").strip(): t.get("ja")
        for t in payload.get("type") or [] if t.get("code")
    }


def fetch(src=None):
    src = src or common.Source()
    try:
        if src.offline:
            payload = src.offline.get_json("aonami-traffic.json")
        else:
            payload = _get_json(TRAFFIC_URL.format(int(time.time() * 1000)))
        station_payload, type_payload = _config(src)
    except Exception as exc:
        print(f"aonami: {type(exc).__name__}: {str(exc)[:120]}")
        return {"ok": False, "fetchedAt": common.jst_now().isoformat(),
                "body": [], "source": "trainlocation-aonamiline.jp"}

    stations = _stations(station_payload)
    types = _types(type_payload)
    up = (payload.get("UP") or [{}])[0]
    body = []
    seen = set()
    # TS first: a train standing at a station has the exact time anchor; an
    # EK copy of the same number (if any) only approximates it.
    for container in ("TS", "EK"):
        for slot in payload.get(container) or []:
            sid = str(slot.get("id") or "")
            m = RE_AT.match(sid)
            if m:
                code, lane = m.group(1), m.group(2)
            else:
                m = RE_EDGE.match(sid)
                if not m:
                    continue
                lane, code = m.group(1), m.group(2)
            station = stations.get(str(int(code)))
            if not station:
                continue
            for tr in slot.get("tr") or []:
                number = str(tr.get("no") or "").strip()
                if not number or number in seen:
                    continue
                seen.add(number)
                try:
                    delay_min = int(float(str(tr.get("dl") or "0").strip()))
                except ValueError:
                    continue
                if delay_min * 60 < DELAY_THRESHOLD_SEC:
                    continue
                dest = stations.get(str(tr.get("ik") or "").strip())
                body.append({
                    "owl:sameAs": f"Aonami:{number}",
                    "odpt:railway": "odpt.Railway:Aonami.AonamiLine",
                    "odpt:trainNumber": number,
                    "odpt:delay": delay_min * 60,
                    "odpt:trainType": types.get(str(tr.get("sy") or "").strip()),
                    "_line_ja": "あおなみ線",
                    "_suki_line": "西名古屋港線",
                    "_station": station,
                    "_dest": dest,
                    "_direction": "下り" if lane == "D" else "上り",
                    "_skip_position_leg": True,
                })
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "source_time": up.get("dt"),
        "body": body,
        "source": "trainlocation-aonamiline.jp",
    }
