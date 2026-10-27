"""Okayama Electric Tramway / Ryobi-group (岡山電気軌道) live feed.

Reverse-engineered 2026-10-27. The position site https://loc.bus-vision.jp/ryobi/
is the Bus-Vision Teeda (Java EE) platform, shared by the Ryobi/Okayama Kido
group. Entry page shell links to:

    approachMap.html / searchStop.html / searchVehicle.html / vehicleState.html

The actual vehicle icons are drawn after a from/to-stop route search:
SearchStop.js calls `Kumu.Ajax.executeTeedaAjax(approachPage_ajaxTrackingVehicle,
{customerCd, planForecastResultCd}, JSON)` and the result carries
{vehicleCorpCd, vehicleCd, iconPass, diff} where `diff` is the delay seconds.

There is **no keyless "all vehicles now" JSON**: every position call rides a
jsessionid Teeda conversation that starts from an entry-page GET. We proved the
entry page is reachable (HTTP 200) and that vehicleState.html without a live
session bounces to announce.html. Per-contract we do not fabricate positions or
delays; body is empty and this is flagged "待实机抓包" for the session flow.
"""

import re
import urllib.request

from . import common

ENTRY = "https://loc.bus-vision.jp/ryobi/"

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
            html = src.offline.root.joinpath("ok-entry.html").read_text(
                encoding="utf-8", errors="replace")
        else:
            html = _get(ENTRY)
        reachable = True
        m = _TITLE_RE.search(html)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()
    except Exception as exc:
        print(f"okayama: {type(exc).__name__}: {str(exc)[:120]}")

    # No keyless per-vehicle position/delay JSON without a Teeda session.
    body = []
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "loc.bus-vision.jp/ryobi",
        "_reachable": reachable,
        "_title": title,
        "_note": ("Bus-Vision Teeda platform; vehicle positions + `diff`(s delay) "
                  "require a sessionized from/to route search, no keyless all-vehicles "
                  "feed -> 待实机抓包, no fabricated rows"),
    }
