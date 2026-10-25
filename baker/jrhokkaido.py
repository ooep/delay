"""JR-Hokkaido (北海道) live train feed for the baker.

Source: https://www3.jrhokkaido.co.jp/trainlocation/location.html
(their own position service, plain JSON, no key):
    /trainlocation/json/rosen/now/rosen_now.json        per-group max delay
    /trainlocation/json/location/now/location_NN_now.json  trains in transit
    /webunkou/json/daiya/daiya_NN.json                  per-senku timetable
    /webunkou/json/master/eki_master.json               station key -> name
All files are UTF-8 with a BOM; missing groups answer 302 and are skipped.

Train identity: {cbango: "4011D", chien: <minutes>, senku: "<NN>", ...}.
senku codes are self-anchored against daiya station lists; the observed
table (00 特急, 02 根室, 05 千歳, 06 函館, 07 日高, 09 富良野, 10 釧網,
11 花咲, 12 学園都市/札沼, 13 石北, 14 宗谷, 15 石勝, 17 室蘭,
19 新幹線) is codified in SENKU_LINES below; any unknown senku still gets
its timetable from its daiya file (matched by train number), only the
suki line filter is lost.

Unlike the other operators this module resolves its own timetable legs
from JR-Hokkaido's daiya files (dokorail's JR-Hokkaido bundles are
timetable-only), so delayed trains carry full stop patterns inline and
delays.py matches them to suki keys without the LegStore.
"""

import json

from . import common

BASE = "https://www3.jrhokkaido.co.jp"
LOCATION_URL = BASE + "/trainlocation/json/location/now/location_{:02d}_now.json"
DAIYA_URL = BASE + "/webunkou/json/daiya/daiya_{}.json"
EKI_URL = BASE + "/webunkou/json/master/eki_master.json"

DELAY_THRESHOLD_MIN = 1

# senku -> jr.suki.ing line name (None = no filter, e.g. the all-line 特急 group)
SENKU_LINES = {
    "00": None,  # 特急 group (hardcoded "00" in their location.js)
    "02": "根室本線",
    "05": "千歳線",
    "06": "函館本線",
    "07": "日高本線",
    "09": "富良野線",
    "10": "釧網本線",
    "11": "花咲線",  # no suki trains yet; board-only
    "12": "学園都市線",  # suki calls it 札沼線 (aliased in baker/suki.py)
    "13": "石北本線",
    "14": "宗谷本線",
    "15": "石勝線",
    "17": "室蘭本線",
    "19": "北海道新幹線",
}


def _get_json_bom(src, url):
    """GET JSON that may carry a UTF-8 BOM; None on any failure."""
    try:
        data = common.http_get(url)
    except Exception:
        return None
    try:
        return json.loads(data.decode("utf-8-sig"))
    except Exception:
        return None


def _offline_json(src, name):
    try:
        return src.offline_json(name)
    except Exception:
        return None


def _fetch_raw(src):
    """Returns (raw_trains, source_time) with raw_trains a list of train
    dicts in transit, source_time the provider's own display timestamp, or
    (None, None) when nothing was reachable."""
    trains = []
    times = []
    if src.offline:
        files = [
            (_offline_json(src, f"jrh-loc-{i:02d}.json")) for i in range(1, 20)
        ]
    else:
        files = [_get_json_bom(src, LOCATION_URL.format(i)) for i in range(1, 20)]
    for d in files:
        if not d or not isinstance(d.get("trains"), list):
            continue
        trains.extend(d["trains"])
        if d.get("time"):
            times.append(d["time"])
    if not trains and not times:
        return None, None
    return trains, (max(times) if times else None)


def parse_raw(raw, src):
    """Raw location trains -> dokorail-feed-shaped payload (ok/fetchedAt/
    body). Delayed trains get their full stop pattern from the daiya
    timetable; on-time trains pass through with odpt:delay 0."""
    eki = None
    body = []
    delayed_seen_senku = set()
    by_number = {}
    for t in raw:
        if not isinstance(t, dict):
            continue
        number = t.get("cbango")
        if not number:
            continue
        chien_min = int(t.get("chien") or 0)
        senku = str(t.get("senku") or "")
        # the feed lists one train under every group its segment spans;
        # keep the max-delay record per train number
        prev = by_number.get(number)
        if prev is not None:
            if chien_min > prev["odpt:delay"] // 60:
                prev["odpt:delay"] = chien_min * 60
                prev["_senku"] = senku
                if chien_min >= DELAY_THRESHOLD_MIN:
                    delayed_seen_senku.add(senku)
            continue
        if chien_min >= DELAY_THRESHOLD_MIN:
            delayed_seen_senku.add(senku)
        entry = {
            "owl:sameAs": f"JR-Hokkaido:{senku}.{number}",
            "odpt:railway": "odpt.Railway:JR-Hokkaido",
            "odpt:trainNumber": str(number),
            "odpt:delay": chien_min * 60,
            "_senku": senku,
        }
        by_number[number] = entry
        body.append(entry)

    if not delayed_seen_senku:
        return {"ok": True, "fetchedAt": common.jst_now().isoformat(), "body": body}

    # stop patterns for delayed trains only
    if src.offline:
        eki_raw = _offline_json(src, "jrh-eki_master.json")
    else:
        eki_raw = _get_json_bom(src, EKI_URL)
    eki = {e["key"]: e["ja"] for e in eki_raw} if isinstance(eki_raw, list) else {}

    daiya = {}
    for senku in delayed_seen_senku:
        if src.offline:
            d = _offline_json(src, f"jrh-daiya_{senku}.json")
        else:
            d = _get_json_bom(src, DAIYA_URL.format(senku))
        if isinstance(d, dict) and isinstance(d.get("today"), list):
            daiya[senku] = {t["cbango"]: t for t in d["today"] if isinstance(t, dict)}

    now_min = common.jst_now().hour * 60 + common.jst_now().minute
    for entry in body:
        if entry["odpt:delay"] < DELAY_THRESHOLD_MIN * 60:
            continue
        # display name vs suki line filter: 特急 (senku 00) spans every line
        entry["_line_ja"] = SENKU_LINES.get(entry["_senku"]) or "特急"
        entry["_suki_line"] = SENKU_LINES.get(entry["_senku"])
        run = daiya.get(entry["_senku"], {}).get(entry["odpt:trainNumber"])
        if not run:
            continue
        stations = [eki.get(st["key"], f"?{st['key']}") for st in run.get("stations", [])]
        if not stations:
            continue
        dep = run["stations"][0].get("time")
        arr = run["stations"][-1].get("time")
        entry["_sched"] = {
            "origin": stations[0],
            "destination": stations[-1],
            "dep": dep,
            "arr": arr if arr != dep else None,
            "stops": stations,
        }
        # worst-case guard: stale daiya row vs live delay
        if dep:
            h, m = dep.split(":")
            lag = now_min - (int(h) * 60 + int(m))
            entry["_stale"] = lag > 360  # >6h past departure = yesterday's row
    return {"ok": True, "fetchedAt": common.jst_now().isoformat(), "body": body}


def fetch(src=None):
    """Feed-shaped payload, or None when the service is unreachable."""
    src = src or common.Source()
    raw, source_time = _fetch_raw(src)
    if not raw:
        return None
    payload = parse_raw(raw, src)
    if payload is not None:
        payload.setdefault("source_time", source_time)
    return payload
