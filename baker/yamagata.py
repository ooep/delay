"""Yamagata Railway (山形鉄道 フラワー長井線) live train feed.

Reverse-engineered 2026-10-27. The public train-tracking page embeds a
Hawkcast (n-sportstracking-lab.com) public map:

    GET https://flower-liner.jp/tracking/
      -> <iframe src="https://hawkcast.n-sportstracking-lab.com/public_maps/831/
            show_without_header?token=X1bzYcpyEQ3uTpFk_yamagata">

The iframe is a React server-rendered page whose `data-react-props` carry:
  - deviceMap[]: the 6 長井線 cars (ダリア/あやめ/花結びより/さくら/紅花/オリジナル)
    each with {id, deviceId, name, group_name:"山形鉄道車両現在位置表示"}.
  - circles[]: every station geofence (name + lat/lng radius 100m).
  - map_ws_endpoint: wss://hawkcast-data-spectators.herokuapp.com/...

Current GPS positions arrive over that WebSocket (each car's `gpses` array is
[] in the SSR payload and is filled by the WS after load). There is **no
keyless REST position feed** and **no delay field** anywhere in the payload.

Per the baker contract we only emit trains carrying odpt:delay >= 60s. This
source exposes position (via WS) but never a delay number, so body is
deliberately empty: we prove the endpoint + car roster, and refuse to invent
a delay. Marked "position feed = WebSocket only; delay not published".
"""

import json
import re
import urllib.request

from . import common

ENTRY = "https://flower-liner.jp/tracking/"
IFRAME_RE = re.compile(
    r'<iframe[^>]+src="(https://hawkcast[^"]+)"', re.I)
# deviceMap car entries are JSON inside the HTML-entity-encoded react-props.
CAR_RE = re.compile(
    r'\{&quot;id&quot;:(\d+),&quot;deviceId&quot;:&quot;([^&]+)&quot;,'
    r'&quot;name&quot;:&quot;([^&]+)&quot;')

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


def _get(url, retries=2, ref=None):
    last = None
    for attempt in range(retries + 1):
        try:
            h = {"User-Agent": UA}
            if ref:
                h["Referer"] = ref
            req = urllib.request.Request(url, headers=h)
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
    cars = []
    iframe_url = None
    try:
        if src.offline:
            html = src.offline.root.joinpath("yg-tracking.html").read_text(
                encoding="utf-8", errors="replace")
        else:
            html = _get(ENTRY)
        m = IFRAME_RE.search(html)
        if m:
            iframe_url = m.group(1).replace("&amp;", "&")
            if not src.offline:
                try:
                    iframe_html = _get(iframe_url, ref=ENTRY)
                    for cid, did, name in CAR_RE.findall(iframe_html):
                        cars.append({"id": cid, "deviceId": did, "name": name})
                except Exception as exc:
                    print(f"yamagata iframe: {type(exc).__name__}: {exc}")
    except Exception as exc:
        print(f"yamagata: {type(exc).__name__}: {str(exc)[:120]}")

    # No delay field is published by this source; never fabricate one.
    body = []
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "hawkcast.n-sportstracking-lab.com/public_maps/831",
        # evidence-only metadata (consumers ignore unknown keys)
        "_cars": cars,
        "_iframe": iframe_url,
        "_note": ("positions via wss only; no delay published -> no body entries"),
    }
