"""Kumamoto City Tram (熊本市交通局 市電ナビ) live feed.

Reverse-engineered 2026-10-27. ASP.NET MVC app at
https://www.kumamoto-city-tram.jp/Sys/web01. The page polls three POST
endpoints (relative to /Sys/, cookie/XHR driven):

    POST /Sys/web01List     [] of in-service trams:
                            {rosen:"A"|"B", us:0|1, interval_id:<int>, rinji_f:0|1}
    POST /Sys/web01Unkyu    [] of suspension/blocked segment ids
    POST /Sys/web01Info     {"TeropLevel","TeropMessage"}  scrolling banner

`rosen` A/B = the two tram systems (A=■A系統 through route, B=■B系統);
`us` 0=U=上り, 1=D=下り; `interval_id` indexes the segment/interval the tram
currently sits in (mapped to on-map polygons in web01.js groupXArray).

Observed 22:45 JST 2026-10-27: web01List == [] (last trams have run);
web01Info returns the fare banner. The feed exposes current segment +
direction but **never a delay number**, so per the contract no body row is
emitted (宁漏不误报). Parser below is live-tested against the real endpoint.
"""

import json
import urllib.request

from . import common

BASE = "https://www.kumamoto-city-tram.jp/Sys/"
LIST_URL = BASE + "web01List"
UNKYU_URL = BASE + "web01Unkyu"
INFO_URL = BASE + "web01Info"

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

DIRECTION = {0: "上り", 1: "下り"}


def _post_json(url, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, data=b"",
                headers={
                    "User-Agent": UA,
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": "https://www.kumamoto-city-tram.jp/Sys/web01",
                    "Content-Type": "application/x-www-form-urlencoded",
                })
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8", "replace") or "[]")
        except Exception as exc:
            last = exc
            if attempt < retries:
                import time
                time.sleep(1.5)
    raise last


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        try:
            trams = src.offline_json("km-list.json")
        except Exception:
            trams = []
        try:
            info = src.offline_json("km-info.json")
        except Exception:
            info = {}
    else:
        try:
            trams = _post_json(LIST_URL)
        except Exception as exc:
            print(f"kumamoto web01List: {type(exc).__name__}: {str(exc)[:120]}")
            trams = []
        try:
            info = _post_json(INFO_URL)
        except Exception:
            info = {}

    body = []  # no delay field in source -> nothing to emit
    cars = []
    for t in trams or []:
        if not isinstance(t, dict):
            continue
        cars.append({
            "rosen": t.get("rosen"),
            "direction": DIRECTION.get(t.get("us"), str(t.get("us"))),
            "interval": t.get("interval_id"),
            "rinji": t.get("rinji_f"),
        })
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "kumamoto-city-tram.jp/Sys/web01List",
        "_cars": cars,
        "_banner": (info or {}).get("TeropMessage") if isinstance(info, dict) else None,
        "_note": ("segment+direction position feed; no delay published -> no body rows"),
    }
