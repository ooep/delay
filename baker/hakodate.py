"""Hakodate City Tram (函館市企業局交通部 市電) live feed via the ICAS
"イカすロケ" position map.

Reverse-engineered 2026-10-28. The map at
    https://icas.hakodatecitytram.com/location/
is a Leaflet shell (leaflet.js + prog/prog-108s.js) that polls, every ~9 s,
the SAME flat CSV-shaped position file family as the モビナビ高知 bus map
(which is buses only and therefore not a rail feed):

    GET https://icas.hakodatecitytram.com/location/IchigoMap.txt
    GET .../Ichigo_stops.csv            (26-stop master, lat/lng)

Vehicle row layout (verified against production client prog-108s.js):
    d[1]   line label (grouping key; ②/⑤ 系統 labels)
    d[3]   GPS receive time, HHMM (server "更新時刻")
    d[5]   lat            d[6]   lng
    d[10]  destination (行先; popup renders "<d[10]> 行き")
    d[11]  current-location text (nearest stop / section)
    d[12]  delay in SECONDS  ->  '+' + parseInt(d[12]/60) + '分' bubble
                                 (official UI: "谷地頭 +5" = 5 min late)
    d[13]  passenger count / crowding

We emit only rows with d[12] >= 60 (>= 1 minute late). d[19] carries the
vehicle's own trip id ("01_1745_2-1-3"): the middle field is the
SCHEDULED origin departure (HHMM) of the run it is working. Verified
live 2026-10-29 17:52 JST on 12 in-service vehicles - each one's
scheduled passage at its current stop (d[16]) sat within 0-2 min of
(d[3] - delay) for exactly that dep0, and no two keys sharing a
terminal share a dep0, so (terminal, dep0) pins the run uniquely.
d[19] runs with no destination (out of service / depot) stay unpinned.
d[16] is the stop the vehicle is at/just left; d[11] is the upcoming
stops ("十字街・函館駅前・..."), which is what `from` reports.
The file is a ~1 minute rolling log (each vehicle appears every ~3 s),
so only the newest row per vehicle is emitted - otherwise one late tram
becomes 15 identical payload rows.
"""

import re
import urllib.request

from . import common

URL = "https://icas.hakodatecitytram.com/location/IchigoMap.txt"
REFERER = "https://icas.hakodatecitytram.com/location/"

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

DELAY_THRESHOLD_SEC = 60


def _get(url, referer, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Referer": referer})
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as exc:
            last = exc
            if attempt < retries:
                import time
                time.sleep(1.5)
    raise last


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        raw = src.offline.root.joinpath("hakodate-ichigo.txt").read_text(
            encoding="utf-8-sig", errors="replace")
    else:
        # cache-buster matches the client's ?HHMMSS poll
        now = common.jst_now()
        raw = _get(f"{URL}?{now.hour:02d}{now.minute:02d}{now.second:02d}",
                   REFERER)
    body = {}
    for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line or line == "END_F" or "END_F" in line:
            continue
        f = line.split(",")
        if len(f) < 13:
            continue  # malformed / comment row
        try:
            delay_sec = int(float(f[12] or 0))
        except ValueError:
            continue
        if delay_sec < DELAY_THRESHOLD_SEC:
            continue
        veh = (f[0] or "").lstrip("\ufeff").strip()
        raw_line = (f[1] or "").strip() or "函館市電"
        # ICAS line column is a system code (8007/7002/9601/...), not a line
        # name; suki models the whole tram as one line (函館市電本線), so the
        # code is normalized away for matching (suki lines_map aliases
        # 函館市電 -> 函館市電本線).
        line_ja = "函館市電" if raw_line.isdigit() else raw_line
        # d[11] is a composite position text ("五稜郭公園前・函館駅前・
        # 十字街・宝来町"): split into single-station candidates so the
        # suki-direct matcher can hit any fragment.
        station_raw = (f[11] or "").strip()
        station_keys = None
        if station_raw:
            keys = [
                s.strip() for s in station_raw.replace("～", "・").split("・")
                if s.strip()
            ]
            station_keys = keys or None
        cur_station = (f[16] or "").strip() if len(f) > 16 else ""
        if not station_keys and cur_station:
            # 无行先预告时退回车辆当前所在站:它同样在本班停站序列里,
            # 且班次号+d[16] 的时刻闸门会再校验一遍。
            station_keys = [cur_station]
        trip_dep0 = None
        if len(f) > 19:
            m = re.match(r"^\d+_(\d{2})(\d{2})_", (f[19] or "").strip())
            if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
                trip_dep0 = int(m.group(1)) * 60 + int(m.group(2))
        row = {
            "owl:sameAs": f"HakodateTram:{f[5]},{f[6]}:{f[10]}",
            "odpt:railway": "odpt.Railway:HakodateTram",
            "odpt:trainNumber": None,
            "odpt:delay": delay_sec,
            "odpt:trainType": None,
            "_line_ja": line_ja,
            "_suki_line": line_ja,
            "_station": station_keys[0] if station_keys else None,
            "_station_keys": station_keys,
            "_cur_station": cur_station or None,
            "_trip_dep0": trip_dep0,
            "_dest": (f[10] or "").strip() or None,
            "_direction": None,
        }
        # rolling log: latest sample per vehicle wins
        body[veh or f"_{len(body)}"] = row
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": list(body.values()),
        "source": "icas.hakodatecitytram.com",
    }
