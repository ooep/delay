"""Tobu (東武) POINT live train feed via the static SSR-HTML pages.

Source: https://static.tobuspapp.jp/html/operation_tobupo/select.html
    -> 8 line pages {lineBit}.html?<unixtime> (20 s full-page refresh, no key).

DOM (reverse-engineered 2026-10-27 against all 8 live pages):
    <div class="stationBlock stationTable">      one row per station, document
        <div class="tableLeft">                  order == track order
            <span class="stationName..." id="TS01"><span class="stationTxt">浅草</span>
        <div class="tableMiddle"></div>         (track gap)
        <div class="tableRight">
            <div class="posLeft">
                <div class="trainPosition1 direction1 trainX1">   train icon
                  <div class="trainName"><span>特急</span></div>  (種別)
                  <div class="trainOption trainOptionDelay">+17</div>  (minutes, optional)
                  <div class="balloonDetailText">浅草行き<br/>    (行先)

  * Each train icon sits INSIDE the station row it is at/nearest to, so a
    document-order walk (kintetsu-style anchor) gives the station directly -
    no position math needed. trainX1/trainX2 are just two parallel icons on
    the same row (e.g. 普通 + 急行 both at 北千住); both inherit the station.
  * direction1 = 上り (confirmed: at 浅草 a direction1 train reads 浅草行き),
    direction2 = 下り.
  * No train numbers are published -> ID-less: trains anchor via
    _station + _dest + _direction into baker/delays.find_leg_by_position.
"""

import re
import time

from . import common

BASE = "https://static.tobuspapp.jp/html/operation_tobupo"
INDEX_URL = BASE + "/select.html"

DELAY_THRESHOLD_MIN = 1  # only emit delay >= 60 s

# lineBit -> (dokorail railway id suffix, ja line name)  (verified vs lines.json)
BIT_MAP = {
    1: ("Tobu.TobuSkytree", "東武スカイツリーライン"),
    16: ("Tobu.Kameido", "亀戸線"),
    32: ("Tobu.Daishi", "大師線"),
    8: ("Tobu.TobuUrbanPark", "東武アーバンパークライン"),
    2: ("Tobu.Isesaki", "伊勢崎線"),
    4: ("Tobu.Nikko", "日光線"),
    4096: ("Tobu.Tojo", "東上線"),
    8192: ("Tobu.Ogose", "越生線"),
}

_STATION_RE = re.compile(
    r'<span class="stationName\w*" id="([A-Z]+\d+)">([\s\S]*?)</span>'
)
_DIV_RE = re.compile(r'<div class="trainPosition\d direction(\d) trainX\d">')
_TNAME_RE = re.compile(r'<div class="trainName">\s*<span>([^<]*)</span>')
_DELAY_RE = re.compile(r'<div class="trainOption trainOptionDelay">([^<]*)</div>')
_DEST_RE = re.compile(r'balloonDetailText">\s*([^<]*?)\s*<br')


def _clean(n):
    """Strip inner tags and the ＜...＞/（...）/※ disambiguators Tobu appends
    to some station names (押上＜スカイツリー前＞※... -> 押上)."""
    n = re.sub(r"<[^>]+>", "", n or "")
    n = re.split(r"[＜(（※]", n)[0]
    return n.replace("\u3000", "").replace(" ", "").strip()


def _dest(n):
    n = _clean(n)
    return re.sub(r"行き$", "", n)


def _parse_page(html):
    """Return [{station, ttype, delay_min, dest, direction}] for one line page."""
    divs = [(m.start(), m.group(1)) for m in _DIV_RE.finditer(html)]
    events = []
    for m in _STATION_RE.finditer(html):
        events.append((m.start(), "st", (m.group(1), _clean(m.group(2)))))
    for i, (pos, direction) in enumerate(divs):
        end = divs[i + 1][0] if i + 1 < len(divs) else len(html)
        chunk = html[pos:end]
        nm = _TNAME_RE.search(chunk)
        dm = _DELAY_RE.search(chunk)
        dsm = _DEST_RE.search(chunk)
        delay_min = 0
        if dm:
            try:
                delay_min = int(dm.group(1).replace("+", "").strip())
            except ValueError:
                delay_min = 0
        events.append(
            (
                pos,
                "tr",
                {
                    "direction": "up" if direction == "1" else "down",
                    "ttype": _clean(nm.group(1)) if nm else None,
                    "delay_min": delay_min,
                    "dest": _dest(dsm.group(1)) if dsm else None,
                },
            )
        )
    events.sort(key=lambda e: e[0])
    anchor = None
    trains = []
    for _pos, kind, val in events:
        if kind == "st":
            anchor = val[1]  # station name (second tuple element)
            continue
        val["station"] = anchor
        trains.append(val)
    return trains


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        pages = []  # fixtures named tobu-<bit>.html; bit encoded in filename
        for f in sorted(src.offline.root.glob("tobu-*.html")):
            m = re.match(r"tobu-(\d+)\.html$", f.name)
            bit = int(m.group(1)) if m else None
            pages.append((bit, f.read_text(encoding="utf-8", errors="ignore")))
    else:
        pages = []
        for bit in BIT_MAP:
            url = f"{BASE}/{bit}.html?{int(time.time())}"
            try:
                html = common.http_get(
                    url, headers={"Referer": INDEX_URL}
                ).decode("utf-8", "ignore")
                pages.append((bit, html))
            except Exception as exc:
                print(f"tobuspapp bit={bit}: {type(exc).__name__}: {str(exc)[:100]}")
    body = []
    for bit, html in pages:
        railway_id, line_ja = BIT_MAP.get(bit, (None, None))
        if not railway_id:
            continue
        for t in _parse_page(html):
            if t["delay_min"] < DELAY_THRESHOLD_MIN or not t["station"]:
                continue
            body.append(
                {
                    "owl:sameAs": f"Tobu-SSR:{railway_id}.{t['direction']}."
                    f"{t['dest']}.{t['station']}",
                    "odpt:railway": f"odpt.Railway:{railway_id}",
                    "odpt:trainNumber": None,
                    "odpt:delay": t["delay_min"] * 60,
                    "odpt:trainType": t["ttype"],
                    "_line_ja": line_ja,
                    "_suki_line": line_ja,
                    "_station": t["station"],
                    "_dest": t["dest"],
                    "_direction": t["direction"],
                }
            )
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "static.tobuspapp.jp/operation_tobupo",
    }
