"""Toyohashi Railroad (豊橋鉄道: 渥美線 + 東田市内線) live train feed.

Reverse-engineered 2026-10-27. Bus-Vision (のってみりん / Ryobi Systems) has
served the two railway lines keyless on the web since the 2026-01-08 GTFS
revision (routes 10_1 = 渥美線, 10_2 = 市内線):

  - Live positions: server-rendered route maps, one embedded JSON blob per live
    vehicle in a hidden div:
      GET https://bus-viewer.jp/toyotetsu/view/approachMap.html?lineMapCd=64   # 渥美線
      GET https://bus-viewer.jp/toyotetsu/view/approachMap.html?lineMapCd=65   # 市内線
    blob = {stopNm, lineNm, passTime, floor, vehicleCorpCd, vehicleCd, ...}
    where passTime = the actual JST time the vehicle last passed stopNm.
  - Per-vehicle destination/headsign:
      GET https://bus-viewer.jp/toyotetsu/view/mapApproachVehicle.html?vehicleCorpCd=&vehicleCd=
    renders 運行情報（渥美線 三河田原行き）plus a fresh server timestamp.

  - Timetable (CC BY 4.0 open data):
      GET https://loc.bus-vision.jp/gtfs_v2/toyohashitetsudo/gtfsFeed   (zip ~120 KB)

Delay is derived per vehicle: actual last-stop passage (passTime) minus the
nearest scheduled departure at that stop on today's calendar service, in the
vehicle's own headsign direction. Emitted only when delay >= 60 s; trains
within the tolerance window are on time and are not output (宁漏不误报).
"""

import csv
import io
import json
import re
import zipfile

from . import common

MAP_BASE = "https://bus-viewer.jp/toyotetsu/view/approachMap.html?lineMapCd={}&lang=0"
VEH_BASE = "https://bus-viewer.jp/toyotetsu/view/mapApproachVehicle.html?vehicleCorpCd={}&vehicleCd={}&lang=0"
GTFS_URL = "https://loc.bus-vision.jp/gtfs_v2/toyohashitetsudo/gtfsFeed"

LINE_MAP_CD = {"渥美線": "64", "市内線": "65"}
# max |actual - scheduled| treated as "same trip" (20 min; service gaps are
# >=16 min on 渥美線 so direction cannot bleed across)
MATCH_WINDOW_SEC = 1200
DELAY_THRESHOLD_SEC = 60

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday",
             "friday", "saturday", "sunday")
# embedded JSON blobs live in <div style="display:none" id="<vehicleCd>">{...}</div>
_BLOB_RE = re.compile(
    r'<div style="display:none" id="\d+">(\{.*?\})\s*</div>', re.S)
_HEADSIGN_RE = re.compile(r'<span id="routeNm">([^<]+)</span>')


def _pass_to_sec(hhmm):
    parts = hhmm.split(":")
    secs = int(parts[2]) if len(parts) > 2 else 0
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + secs


def _load_gtfs():
    """Parse the GTFS static zip into timetable indexes for today's service."""
    raw = common.http_get(GTFS_URL, retries=2)
    zf = zipfile.ZipFile(io.BytesIO(raw))

    def rows(name):
        with zf.open(name) as fh:
            yield from csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig"))

    # today's service_id from calendar.txt (Monday=0 ... Sunday=6)
    wd = common.jst_now().weekday()
    service_ids = set()
    for r in rows("calendar.txt"):
        if r[_WEEKDAYS[wd]] == "1":
            service_ids.add(r["service_id"])
    # service exceptions (calendar_dates.txt: added/removed) — best effort
    today_iso = common.jst_now().date().strftime("%Y%m%d")
    try:
        for r in rows("calendar_dates.txt"):
            if r.get("date") != today_iso:
                continue
            if r.get("exception_type") == "1":
                service_ids.add(r["service_id"])
            elif r.get("exception_type") == "2":
                service_ids.discard(r["service_id"])
    except KeyError:
        pass

    stops = {}          # stop_name -> stop_id
    sid_name = {}       # stop_id -> stop_name
    for r in rows("stops.txt"):
        stops[r["stop_name"]] = r["stop_id"]
        sid_name[r["stop_id"]] = r["stop_name"]

    trips = {}          # trip_id -> {service, headsign, route_id}
    for r in rows("trips.txt"):
        trips[r["trip_id"]] = {
            "service": r["service_id"],
            "headsign": r["trip_headsign"],
            "route_id": r["route_id"],
        }

    # per-stop scheduled departures, restricted to today's services:
    # (stop_id, headsign) -> sorted [(dep_sec, trip_id)]
    by_stop = {}
    trip_stops = {}     # trip_id -> [(seq, stop_id, dep_sec)]
    for r in rows("stop_times.txt"):
        t = trips.get(r["trip_id"])
        if not t or t["service"] not in service_ids:
            continue
        dep = _pass_to_sec(r["departure_time"])
        by_stop.setdefault((r["stop_id"], t["headsign"]), []).append(
            (dep, r["trip_id"]))
        trip_stops.setdefault(r["trip_id"], []).append(
            (int(r["stop_sequence"]), r["stop_id"], dep))
    for v in by_stop.values():
        v.sort()
    return stops, sid_name, trips, by_stop, trip_stops


def _vehicles_on_map(line_map_cd):
    html = common.get_text(MAP_BASE.format(line_map_cd), retries=2)
    out = []
    for m in _BLOB_RE.finditer(html):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        for it in data.get("appraochMapItems") or []:
            out.append(it)
    return out


def _headsign_of(corp_cd, veh_cd):
    """Rendered per-vehicle page: 運行情報（渥美線 三河田原行き）."""
    try:
        html = common.get_text(VEH_BASE.format(corp_cd, veh_cd), retries=2)
    except Exception:
        return None
    m = _HEADSIGN_RE.search(html)
    if not m:
        return None
    text = m.group(1).replace("行き", "").strip()
    parts = text.split()
    return parts[-1].strip() if parts else None


def fetch(src=None):
    src = src or common.Source()
    try:
        stops, sid_name, trips, by_stop, trip_stops = _load_gtfs()
    except Exception as exc:  # timetable unavailable -> no delay anchor
        print(f"toyohashi gtfs: {type(exc).__name__}: {str(exc)[:120]}")
        return {"ok": False, "fetchedAt": common.jst_now().isoformat(),
                "body": [], "source": "loc.bus-vision.jp/toyohashitetsudo"}

    name_to_stop = stops
    body = []
    seen = set()
    for line_ja, cd in LINE_MAP_CD.items():
        try:
            vehicles = _vehicles_on_map(cd)
        except Exception as exc:
            print(f"toyohashi map {cd}: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        for v in vehicles:
            stop_nm = v.get("stopNm")
            pass_t = v.get("passTime")
            if not stop_nm or not pass_t or ":" not in pass_t:
                continue
            stop_id = name_to_stop.get(stop_nm)
            if not stop_id:
                continue
            actual = _pass_to_sec(pass_t)
            headsign = _headsign_of(v.get("vehicleCorpCd"), v.get("vehicleCd"))
            # candidate scheduled departures at this stop in the vehicle's direction
            cands = []
            for (sid, hs), lst in by_stop.items():
                if sid != stop_id:
                    continue
                if headsign and not hs.startswith(headsign):
                    continue
                cands.extend(lst)
            if not cands:
                continue
            dep_sec, trip_id = min(cands, key=lambda c: abs(c[0] - actual))
            delay = actual - dep_sec
            if delay < DELAY_THRESHOLD_SEC or abs(delay) > MATCH_WINDOW_SEC:
                continue  # on time (or unmatched) -> not output
            seq = sorted(trip_stops.get(trip_id, []))
            trip_hs = trips[trip_id]["headsign"]
            # resolve_direct_by_time 的 official 口径: departure = 报站
            # 的发车分钟, stations = 报站之后的停站。seq 是全程表, 旧写法
            # 拿首站时间当 departure、整条表当 stations, 口径对不上, 这条
            # 线的延误永远钉不上班次。
            anchor = next((i for i, (_, sid, _) in enumerate(seq) if sid == stop_id), None)
            off = None
            if anchor is not None:
                off = {
                    "departure": seq[anchor][2] // 60,
                    "stations": [sid_name.get(sid, sid) for _, sid, _ in seq[anchor + 1:]],
                    "terminal": trip_hs,
                }
            direction = ("下り" if any(k in trip_hs for k in
                                      ("三河田原", "運動公園前", "競輪場前", "赤岩口"))
                         else "上り")
            key = (trip_id, stop_nm)
            if key in seen:
                continue
            seen.add(key)
            body.append({
                "owl:sameAs": f"Toyohashi:{line_ja}:{stop_nm}:{headsign or trip_hs}",
                "odpt:railway": f"odpt.Railway:Toyohashi.{line_ja}",
                "odpt:trainNumber": v.get("floor"),
                "odpt:delay": int(delay),
                "odpt:trainType": "普通",
                "_line_ja": line_ja,
                "_suki_line": line_ja,
                "_station": stop_nm,
                "_dest": headsign or trip_hs,
                "_direction": direction,
                "_official": off,
            })
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "bus-viewer.jp/toyotetsu",
    }
