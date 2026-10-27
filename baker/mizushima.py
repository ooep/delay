"""Mizushima Rinkai Railway (水島臨海鉄道) live feed.

Reverse-engineered 2026-10-27. Same Bus-Vision Teeda platform as Okayama
(see baker/okayama.py) but the `mizurin` provider:

    ENTRY https://loc.bus-vision.jp/mizurin/
    shares /mizurin/view/js/{common,sp}/*.js and the approachMap/searchVehicle
    route-search flow; per-vehicle delay arrives as `diff` seconds in the
    approachPage_ajaxTrackingVehicle JSON after a sessionized from/to search.

No keyless all-vehicle JSON. Entry page confirmed reachable (HTTP 200).
No positions/delays fabricated; body empty, flagged 待实机抓包.
"""

import re
import urllib.request

from . import common

ENTRY = "https://loc.bus-vision.jp/mizurin/"

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)


def _get(url, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
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
    reachable = False
    title = None
    try:
        if src.offline:
            html = src.offline.root.joinpath("mz-entry.html").read_text(
                encoding="utf-8", errors="replace")
        else:
            html = _get(ENTRY)
        reachable = True
        m = _TITLE_RE.search(html)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()
    except Exception as exc:
        print(f"mizushima: {type(exc).__name__}: {str(exc)[:120]}")

    body = []
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "loc.bus-vision.jp/mizurin",
        "_reachable": reachable,
        "_title": title,
        "_note": ("Bus-Vision Teeda platform (mizurin provider); per-vehicle positions "
                  "and `diff`(s delay) need a sessionized route search, no keyless "
                  "all-vehicles feed -> 待实机抓包"),
    }
