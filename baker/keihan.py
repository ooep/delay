"""Keihan (京阪電鉄) live train feed for the baker.

Source: the 在線位置 page's own JSON (no key, no WAF; file rolls every
minute - deep-night snapshots simply have empty arrays):
    https://www.keihan.co.jp/zaisen-up/trainPositionList.json
    (discovered from /zaisen/common/js/script.es6.js: trainPositionFile)

Payload shape (page JS + captured sample; arrays empty when no trains):
    {fileCreatedTime, fileVersion, linkNum,
     TrainInfo: [...]                       # early-morning first-dep style
     locationObjects: [                      # grid cells, one per position
       {...cell coords...,
        trainInfoObjects: [{
          trainNumber, delayMinutes,           # both present - ID-ful!
          trainTypeJp, destStationNameJp, carsOfTrain,
          lastPassStation, trainTypeIcon...
        }]
      }],
     }
Some records also carry diaStationInfoObjects - the train's own stop list
with per-station departure times (multi-lang station names). When present
the entry is fully self-contained (stops+times inline, like JR-Hokkaido's
daiya); otherwise the position-time engine resolves the dokorail leg from
the reported cell/lastPassStation.

Keihan dokorail railway ids (title bridge needs these - the main line is
Keihan.MainLineOto, "京阪電気鉄道京阪本線・鴫野線"): MainLineOto (本線+鴫野線),
Nakanoshima (中之島線), Uji (宇治線), Katano (交野線).
"""

import re

from . import common

BASE = "https://www.keihan.co.jp"
POSITION_URL = BASE + "/zaisen-up/trainPositionList.json"

DELAY_THRESHOLD_MIN = 1

# Keihan's delayMinutes is a STRING that may carry the unit ("6分遅れ",
# ""), or a plain number depending on the file roll - parse both safely.
_DELAY_RE = re.compile(r"(\d+)")


def _delay_minutes(v):
    if isinstance(v, (int, float)):
        return int(v)
    if v is None:
        return 0
    m = _DELAY_RE.match(str(v).strip())
    return int(m.group(1)) if m else 0

# board line name + dokorail railways for legs (order = search priority)
KEIHAN_RAILWAYS = [
    "Keihan.MainLineOto",
    "Keihan.Nakanoshima",
    "Keihan.Uji",
    "Keihan.Katano",
]


def _collect(payload):
    """Yield train records from either shape (grid cells / flat list)."""
    if not isinstance(payload, dict):
        return
    for cell in payload.get("locationObjects") or []:
        if not isinstance(cell, dict):
            continue
        for t in cell.get("trainInfoObjects") or []:
            if isinstance(t, dict):
                yield t, cell
    for t in payload.get("TrainInfo") or []:
        if isinstance(t, dict):
            yield t, None


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        try:
            d = src.offline_json("kh-tp.json")
        except Exception:
            return None
    else:
        try:
            data = common.http_get(POSITION_URL, headers={"Referer": BASE + "/zaisen/"})
            d = json.loads(data.decode("utf-8-sig"))
        except Exception as exc:
            print(f"keihan feed: {type(exc).__name__}: {str(exc)[:120]}")
            return None

    body = []
    for t, _cell in _collect(d):
        number = str(t.get("trainNumber") or "").strip()
        delay_min = _delay_minutes(t.get("delayMinutes"))
        if not number and delay_min < DELAY_THRESHOLD_MIN:
            continue
        if not number:
            continue  # ID-less cells resolve via position engine elsewhere
        dia = t.get("diaStationInfoObjects") or []
        # lastPassStation is a line-specific station CODE (e.g. "18"), not a
        # name - only useful to the position engine when a name master lands
        last_pass = t.get("lastPassStation")
        entry = {
            "owl:sameAs": f"Keihan:{number}",
            "odpt:railway": "odpt.Railway:Keihan",
            "odpt:trainNumber": number,
            "odpt:delay": delay_min * 60,
            "odpt:trainType": t.get("trainTypeJp"),
            "_railways": KEIHAN_RAILWAYS,
            "_line_ja": "京阪本線",
            "_suki_line": "京阪本線",
            "_station": last_pass if isinstance(last_pass, str) and not last_pass.isdigit() else None,
        }
        if dia:
            stops = [s.get("stationNameJp") for s in dia if s.get("stationNameJp")]
            if len(stops) >= 2:
                times = [s.get("stationDepTime") for s in dia if s.get("stationNameJp")]
                entry["_sched"] = {
                    "dep": next((x for x in times if x and x != "-"), None),
                    "arr": next((x for x in reversed(times) if x and x != "-"), None),
                    "origin": stops[0],
                    "destination": stops[-1],
                    "stops": stops,
                }
        body.append(entry)
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "keihan.co.jp/zaisen-up",
    }


import json  # noqa: E402
