"""Hapi-Line Fukui (ハピラインふくい) live train feed via ハピラインNAVI.

Reverse-engineered 2026-10-30 from the desktop map
(https://trainlocation.hapi-line.co.jp/). The page is a Nuxt SPA — the HTML
has no train data; the map is fed by:

    GET /data/traffic_info.json?<ms>
        {"UP":[{"dt":"2026/10/30 12:19:39","st":"0"}],
         "TS":[{"id":"E114D","tr":[{"no":"1335M","bs":"3","sy":"1",
                                    "ik":"214","dl":"0","hk":"1"}]}],
         "EK":[{"id":"D116","tr":[...]}, ...]}
    GET /config/station.json    {"ikisaki":[{"code":"114","name":"福井"}, ...]}
    GET /config/car_type.json   {"type":[{"code":"1","name":"普通"}, ...]}

Slot ids name the position marker: "E###D"/"E###U" (TS) = standing at
station ###, "D###"/"U###" (EK) = on the segment approaching station ###;
verified by polling 1243M: EK D102 → TS E103D → EK D107 → TS E107D, and
1335M: TS E114D → EK D116. D = 下り (codes increasing, toward 福井/金沢),
U = 上り (toward 敦賀). `dl` is the delay in MINUTES, `ik` the destination
station code (`ikisaki`, 1xx Hapi-Line / 2xx+ JR-West through section) and
`sy` the car type; 貨物/回送等 (3/4) are not passenger services and are
dropped.

The host only accepts connections from Japanese IPs, so HAPILINE_BASE points
the baker at suki-jp-relay: a separate Cloudflare Worker pinned near Tokyo
that only accepts GitHub Actions OIDC tokens minted for ooep/delay's bake
workflow. Unset -> hits the origin directly (local runs).
"""

import json
import os
import re
import time

from . import common

ORIGIN = "https://trainlocation.hapi-line.co.jp"
BASE = os.environ.get("HAPILINE_BASE", ORIGIN).rstrip("/")
TRAFFIC_URL = BASE + "/data/traffic_info.json?{}"
STATION_URL = BASE + "/config/station.json"
CARTYPE_URL = BASE + "/config/car_type.json"

DELAY_THRESHOLD_SEC = 60
SKIP_TYPES = {"3", "4"}   # 貨物 / 回送等

RE_AT = re.compile(r"^E(\d{3})([DU])$")    # train at station ###
RE_EDGE = re.compile(r"^([DU])(\d{3})$")   # on the segment approaching ###

_cfg = {}


def _auth_headers():
    # relay mode: the Worker demands the shared gate key plus a short-lived
    # GitHub OIDC bearer; direct origin runs (local JP egress) need neither
    if BASE == ORIGIN:
        return None
    headers = {}
    key = common.jp_relay_key()
    if key:
        # the deployed gate reads x-suki-relay-key, older builds read
        # X-Relay-Key; send both so either Worker build accepts the bake
        headers["X-Relay-Key"] = key
        headers["X-Suki-Relay-Key"] = key
    token = common.jp_relay_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers or None


def _get_json(url, retries=3):
    return json.loads(common.http_get(url, headers=_auth_headers(), retries=retries,
                                      timeout=25).decode("utf-8", "replace"))


def _config(src):
    if src.offline:
        return (src.offline.get_json("hapiline-station.json"),
                src.offline.get_json("hapiline-cartype.json"))
    if "station" not in _cfg or "type" not in _cfg:
        station = _get_json(STATION_URL)
        cartype = _get_json(CARTYPE_URL)
        _cfg["station"], _cfg["type"] = station, cartype
    return _cfg["station"], _cfg["type"]


def _stations(payload):
    out = {}
    for s in payload.get("ikisaki") or []:
        code = str(s.get("code") or "").strip()
        name = s.get("name")
        if code and name:
            out.setdefault(code, name.strip())
    return out


def _types(payload):
    return {
        str(t.get("code") or "").strip(): t.get("name")
        for t in payload.get("type") or [] if t.get("code")
    }


def fetch(src=None):
    src = src or common.Source()
    try:
        if src.offline:
            payload = src.offline.get_json("hapiline-traffic.json")
        else:
            payload = _get_json(TRAFFIC_URL.format(int(time.time() * 1000)))
        station_payload, type_payload = _config(src)
    except Exception as exc:
        print(f"hapi-line: {type(exc).__name__}: {str(exc)[:120]}")
        return {"ok": False, "fetchedAt": common.jst_now().isoformat(),
                "body": [], "source": "trainlocation.hapi-line.co.jp"}

    stations = _stations(station_payload)
    types = _types(type_payload)
    up = (payload.get("UP") or [{}])[0]
    body = []
    seen = set()
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
                sy = str(tr.get("sy") or "").strip()
                if sy in SKIP_TYPES:
                    continue
                try:
                    delay_min = int(float(str(tr.get("dl") or "0").strip()))
                except ValueError:
                    continue
                if delay_min * 60 < DELAY_THRESHOLD_SEC:
                    continue
                train_type = types.get(sy)
                if train_type == "情報なし":
                    train_type = None
                body.append({
                    "owl:sameAs": f"HapiLine:{number}",
                    "odpt:railway": "odpt.Railway:HapilineFukui.HapilineFukui",
                    "odpt:trainNumber": number,
                    "odpt:delay": delay_min * 60,
                    "odpt:trainType": train_type,
                    "_line_ja": "ハピラインふくい線",
                    "_suki_line": "ハピラインふくい線",
                    "_station": station,
                    "_dest": stations.get(str(tr.get("ik") or "").strip()),
                    "_direction": "下り" if lane == "D" else "上り",
                    "_skip_position_leg": True,
                })
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "source_time": up.get("dt"),
        "body": body,
        "source": "trainlocation.hapi-line.co.jp",
    }
