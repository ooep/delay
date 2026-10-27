"""Hakodate City Tram (函館市企業局交通部 市電) live feed via the ICAS
"イカすロケ" position map.

Reverse-engineered 2026-10-28. The map at
    https://icas.hakodatecitytram.com/location/
is a Leaflet shell (leaflet.js + prog/prog-108s.js) that polls, every ~9 s,
the SAME flat CSV-shaped position file family already used by tosaden.py:

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

We emit only rows with d[12] >= 60 (>= 1 minute late). No train number /
train type / per-train timetable is exposed by this endpoint, so those
fields stay None (宁漏不误报). Probe-time note: IchigoMap.txt returned
only the "BOM END_F" terminator at 01:1x JST (service ended for the
night), so no live vehicle row was captured; the parser is defensive
about short rows and is byte-identical in shape to the production client.
"""

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
    body = []
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
        line_ja = (f[1] or "").strip() or "函館市電"
        body.append({
            "owl:sameAs": f"HakodateTram:{f[5]},{f[6]}:{f[10]}",
            "odpt:railway": "odpt.Railway:HakodateTram",
            "odpt:trainNumber": None,
            "odpt:delay": delay_sec,
            "odpt:trainType": None,
            "_line_ja": line_ja,
            "_suki_line": line_ja,
            "_station": (f[11] or "").strip() or None,
            "_dest": (f[10] or "").strip() or None,
            "_direction": None,
        })
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "icas.hakodatecitytram.com",
    }
