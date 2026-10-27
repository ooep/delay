"""IR Ishikawa Railway (ＩＲいしかわ鉄道) live feed for the baker.

The operator runs its own React SPA at https://www.ishikawa-railway.jp/ishi-train/
which talks to a small ODPT-shaped JSON API:

    GET https://www.ishikawa-railway.jp/api/v1/trains       live trains
    GET https://www.ishikawa-railway.jp/api/v1/definitions station/type/direction dict
    GET https://www.ishikawa-railway.jp/api/v1/messages     line-level status text
    GET https://www.ishikawa-railway.jp/api/v1/timetables/<trainNumber>  per-train stops

/trains returns an array whose first element at night is a header marker
record ({"@type":"odpt:Train","@id":"info:<date>","@dc:date":...,"odpt:frequency":30});
actual trains carry odpt:trainNumber. Live train records (observed via the
frontend bundle) are ODPT-shaped: odpt:trainNumber, odpt:delay (seconds),
odpt:trainType, odpt:railway, odpt:fromStation / odpt:toStation,
odpt:railDirection, odpt:startingStation / odpt:terminalStation. Station
and direction URIs resolve through /definitions.

Only delay >= 60 s is emitted. Out-of-service hours leave /trains with the
header marker record alone — an empty body is the honest answer.
"""

import json
import urllib.request

from . import common

BASE = "https://www.ishikawa-railway.jp/api/v1/"
TRAINS_URL = BASE + "trains"
DEFS_URL = BASE + "definitions"

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

DELAY_THRESHOLD_SEC = 60


def _get_json(url, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Referer": "https://www.ishikawa-railway.jp/ishi-train/",
            })
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as exc:
            last = exc
            if attempt < retries:
                import time
                time.sleep(1.5)
    raise last


def _direction(rd_ja):
    """下り = 大聖寺・福井方面, 上り = 金沢・富山方面."""
    if not rd_ja:
        return None
    if "福井" in rd_ja:
        return "下り"
    if "富山" in rd_ja:
        return "上り"
    return None


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        trains = src.offline_json("ir-trains.json")
        defs = src.offline_json("ir-defs.json")
    else:
        try:
            trains = _get_json(TRAINS_URL)
            defs = _get_json(DEFS_URL)
        except Exception as exc:
            print(f"ishikawa: {type(exc).__name__}: {str(exc)[:120]}")
            return {"ok": False, "fetchedAt": common.jst_now().isoformat(),
                    "body": [], "source": "ishikawa-railway.jp/api"}

    stations, types, directions = {}, {}, {}
    for block in defs or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("@type")
        if btype == "odpt:Station":
            stations = {k: v for k, v in block.items() if k != "@type"}
        elif btype == "odpt:TrainType":
            types = {k: v for k, v in block.items() if k != "@type"}
        elif btype == "odpt:RailDirection":
            directions = {k: v for k, v in block.items() if k != "@type"}

    body = []
    for tr in trains or []:
        if not isinstance(tr, dict):
            continue
        number = tr.get("odpt:trainNumber")
        if not number:  # skip the info: header marker record
            continue
        delay = int(tr.get("odpt:delay") or 0)
        if delay < DELAY_THRESHOLD_SEC:
            continue
        rd = tr.get("odpt:railDirection")
        body.append({
            "owl:sameAs": f"IRIshikawa:{number}",
            "odpt:railway": tr.get("odpt:railway",
                                    "odpt.Railway:IRIshikawa.IRIshikawa"),
            "odpt:trainNumber": str(number),
            "odpt:delay": delay,
            "odpt:trainType": types.get(tr.get("odpt:trainType"),
                                        tr.get("odpt:trainType")),
            "_line_ja": "ＩＲいしかわ線",
            "_suki_line": "ＩＲいしかわ線",
            "_station": stations.get(tr.get("odpt:fromStation")),
            "_dest": stations.get(tr.get("odpt:terminalStation")) or
                     stations.get(tr.get("odpt:toStation")),
            "_direction": _direction(directions.get(rd)),
        })
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "ishikawa-railway.jp/api/v1",
    }
