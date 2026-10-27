"""Hiroden (広島電鉄 路面電車) live feed.

Reverse-engineered 2026-10-27. The public 電車位置情報 map at
https://location.hiroden.co.jp/ is a canvas map whose XHR CGI layer (driven by
js/mapV3.js) is station-click driven:

    GET /system/get_dentei_id_countV3.cgi?d=&x=<px>&y=<px>&s=   -> count
    GET /system/get_dentei_listV3.cgi?d=&x=<px>&y=<px>&s=      -> "<id>\\t<id>"
    GET /system/get_touchyaku_densya_list3V3.cgi?d=<id>&x&y&s&m=1
        -> first line "OK\\t<flag>\\t<note>", then an HTML fragment:
           <span class="stopName">舟入川口町<span>(広島駅･横川方面)</span></span>
           + <tbody> rows of 号線/行先/車種 | 予定時刻

There is **no global "all trams" feed** and **no explicit delay field**: the CGI
returns predicted arrival clock-times at one named stop. Delay would have to be
derived by diffing predicted vs scheduled, which the baker forbids fabricating.
Observed 22:50 JST 2026-10-27: "データ無し" (service ended). We probe a small
station-id sample to prove the interface and parse the stop name, but emit no
body rows (no delay number).
"""

import re
import urllib.parse
import urllib.request

from . import common

BASE = "https://location.hiroden.co.jp/system/"
ARRIVAL_CGI = BASE + "get_touchyaku_densya_list3V3.cgi"
# Station ids probed live (舟入川口町 == 1020 discovered via get_dentei_listV3).
PROBE_IDS = ["1020", "1030"]

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


def _get(url, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA,
                              "Referer": "https://location.hiroden.co.jp/index.html"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as exc:
            last = exc
            if attempt < retries:
                import time
                time.sleep(1.5)
    raise last


_STOP_RE = re.compile(r'<span class="stopName">([^<]+)(?:<span>\(([^)]+)\)</span>)?')


def _parse(frag):
    """Return (stop_name, direction) from the HTML fragment."""
    m = _STOP_RE.search(frag)
    if not m:
        return None, None
    return m.group(1).strip(), (m.group(2) or None)


def fetch(src=None):
    src = src or common.Source()
    probed = []
    for sid in PROBE_IDS:
        try:
            if src.offline:
                frag = src.offline.root.joinpath(f"hd-{sid}.html").read_text(
                    encoding="utf-8", errors="replace")
            else:
                qs = urllib.parse.urlencode(
                    {"d": sid, "x": 400, "y": 400, "s": "", "m": 1})
                frag = _get(ARRIVAL_CGI + "?" + qs)
            stop, direction = _parse(frag)
            probed.append({"id": sid, "stop": stop, "direction": direction,
                           "has_data": "データ無し" not in frag})
        except Exception as exc:
            print(f"hiroden {sid}: {type(exc).__name__}: {str(exc)[:100]}")
    # No delay field in source -> no body rows (arrival clock-time is not a delay).
    body = []
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "location.hiroden.co.jp/system/get_touchyaku_densya_list3V3.cgi",
        "_probed": probed,
        "_note": ("per-stop predicted-arrival CGI; no explicit delay -> no body rows; "
                  "needs daytime station-id census + scheduled-time diff to derive delay"),
    }
