"""Kintetsu (近畿日本鉄道) live train feed for the baker.

Source: the PC 走行位置 pages, server-rendered HTML (no key):
    https://tid.kintetsu.co.jp/LocationWeb/index.html
        -> links trainlocationinfowebNN.html, 20 line pages
    https://tid.kintetsu.co.jp/LocationWeb/trainlocationinfowebNN.html

Each page carries:
    <h2><span>【A】奈良線</span>(<span>大阪難波方面</span>〜...)</h2>   line name
    <div class="station-box-up" data-stationid="A01" ...>
        <p class="station-name">大阪難波</p>                          track order
    <div class="train-info-up|-down">                                  trains
        .train-destination-text (行先), <span class="delay-time">
        (delay in SECONDS, e.g. 255 = 4 min 15 s)

No train numbers are published -> ID-less: a train anchors to the nearest
preceding station-box; direction from the up/down class; destination from
the text. All feed the position-time engine
(baker/delays.find_leg_by_position).
"""

import re

from . import common

BASE = "https://tid.kintetsu.co.jp/LocationWeb"
INDEX_URL = BASE + "/index.html"

DELAY_THRESHOLD_MIN = 1

_STATION_RE = re.compile(
    r'data-stationid="([A-Z]\d+)"[\s\S]{0,200}?<p class="station-name">([^<]+)</p>',
    re.S,
)
_CELL_RE = re.compile(r'data-stationcode="(\d+)"')
_TRAIN_RE = re.compile(
    r'class="train-info-(up|down)"[\s\S]{0,400}?'
    r'train-destination-text[^>]*>([^<]+)<[\s\S]{0,300}?'
    r'class="delay-time">([^<]*)<',
    re.S,
)
_LINE_RE = re.compile(r"<h2><span>【[A-Z]】([^<]+)</span>")


def _html(url):
    return common.http_get(url, headers={"Referer": BASE + "/"}).decode("utf-8", "ignore")


def _strip_spaces(name):
    return (name or "").replace("\u3000", "").replace(" ", "").strip()


def _parse_line(html):
    m = _LINE_RE.search(html)
    line_name = _strip_spaces(m.group(1)) if m else None
    # ordered station names from the layout strip (document order = track
    # order); trains live in a separate grid afterwards, keyed by
    # data-stationcode="N" (1-based index into this list)
    ordered = [_strip_spaces(mm.group(2)) for mm in _STATION_RE.finditer(html)]
    events = []
    for mm in _CELL_RE.finditer(html):
        events.append((mm.start(), "cell", int(mm.group(1))))
    for mm in _TRAIN_RE.finditer(html):
        events.append((mm.start(), "train", mm))
    events.sort(key=lambda e: e[0])
    trains = []
    anchor = None
    for _pos, kind, val in events:
        if kind == "cell":
            anchor = val
            continue
        direction, dest, delay_s = val.group(1), val.group(2), val.group(3)
        try:
            delay_min = round(int(float(delay_s or 0)) / 60)
        except ValueError:
            delay_min = 0
        station = ordered[anchor - 1] if anchor and 0 < anchor <= len(ordered) else None
        trains.append(
            {
                "station": station,
                "dest": _strip_spaces(dest),
                "delay_min": delay_min,
                "direction": direction,
            }
        )
    return line_name, trains


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        pages = []
        for f in sorted(src.offline_root.glob("kt-w*.html")):
            pages.append(f.read_text(encoding="utf-8", errors="ignore"))
    else:
        try:
            index_html = _html(INDEX_URL)
        except Exception as exc:
            print(f"kintetsu index: {type(exc).__name__}: {str(exc)[:100]}")
            return None
        files = sorted(set(re.findall(r'(trainlocationinfoweb\d+\.html)', index_html)))
        pages = []
        for f in files:
            try:
                pages.append(_html(f"{BASE}/{f}"))
            except Exception:
                continue

    body = []
    for html in pages:
        line_name, trains = _parse_line(html)
        for t in trains:
            if t["delay_min"] < DELAY_THRESHOLD_MIN or not t["station"]:
                continue
            body.append(
                {
                    "owl:sameAs": f"Kintetsu:{line_name}.{t['direction']}.{t['dest']}.{t['station']}",
                    "odpt:railway": "odpt:Railway:Kintetsu",
                    "odpt:trainNumber": None,
                    "odpt:delay": t["delay_min"] * 60,
                    "odpt:trainType": None,
                    "_line_ja": line_name,
                    "_suki_line": line_name,
                    "_station": t["station"],
                    "_dest": t["dest"],
                }
            )
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "tid.kintetsu.co.jp/LocationWeb",
    }
