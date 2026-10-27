"""Tosaden Kotsu (とさでん交通) live tram feed.

Source: the とさでんロケマップ at
    https://tosaden-rb.kochi-mobility.net/location/
which polls, every 5 s, a flat CSV-shaped position file:
    GET https://tosaden-rb.kochi-mobility.net/location/IchigoMap.txt?<HHMMSS>
    GET .../Ichigo_stops.csv                       (stop master, lat/lon)

Row layout was taken from the site's own client script (prog/prog-092.js,
parses each row with split(',') and renders the Leaflet popup):
    d[1]   line label (green text in popup, e.g. 【伊野線】系)
    d[3]   GPS receive time, HHMM (server "更新時刻")
    d[5]   lat            d[6]   lng
    d[8]   crowding flag
    d[10]  destination ("<d[10]> 行き")
    d[11]  current-location text (nearest stop / section)
    d[12]  delay in SECONDS  ->  parseInt(d[12]/60) + "分遅れ" on the popup
    d[13]  passenger count  d[19] timetable key

We emit only rows with d[12] >= 60 (>= 1 minute late). No train number /
train type / station timetable is exposed by this endpoint, so those fields
stay None (宁漏不误报). Probe-time note: IchigoMap.txt returned only the
"BOM END_F" terminator at 23:50 JST (service ended for the night), so no
live vehicle row was captured; the field map above is from the production
client script and the parser is defensive about short rows.
"""

import urllib.request

from . import common

URL = "https://tosaden-rb.kochi-mobility.net/location/IchigoMap.txt"
REFERER = "https://tosaden-rb.kochi-mobility.net/location/"

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
        raw = src.offline.root.joinpath("tosaden-ichigo.txt").read_text(
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
        line_ja = (f[1] or "").strip() or "とさでん交通"
        body.append({
            "owl:sameAs": f"Tosaden:{f[5]},{f[6]}:{f[10]}",
            "odpt:railway": "odpt.Railway:Tosaden",
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
        "source": "tosaden-rb.kochi-mobility.net",
    }
