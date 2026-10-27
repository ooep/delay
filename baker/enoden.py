"""Enoden (江ノ島電鉄) live-position feed.

Source: the NEC-built position site https://www.enoden-location.jp/
(mobile mode only; redirects / -> /Home/Index?mode=1). Polled every 3 s:

    GET /Home/TrainList?mode=1  -> JSON array
    POST /Home/web01Info       -> {"teropLevel","teropMessage"} line ticker
    POST /Home/web01Unkyu      -> suspension list

Train row: {adjustment, retsuBng:" 11", usF:0|1, point_x, point_y,
            main:{no,type}, sub:{no,type}}
  retsuBng = 列番, usF 1=U(上り 藤沢方面) 0=D(下り 鎌倉方面),
  point_x/y = pixel position on the static schematic /img/train_m/smart_jp.png
  (1627x9176, EN01 藤沢 at top -> EN15 鎌倉 at bottom).

BLOCKER (checked 2026-10-27 23:55 JST): this site distributes position and
列番 only. There is NO per-train delay field (no delay/delayMinute key in
TrainList), no station/destination names, and the terop ticker carries at
most a free-text line-level message (empty / "通常運行" when normal). Per
the baker contract (emit only odpt:delay >= 60 s, 宁漏不误报, do not
fabricate) this feed therefore returns body=[] until a delay signal is
confirmed by real-train capture. It is marked データ存疑: the position
parser below is production-ready, but delay is not delivered here.
"""

import json
import urllib.request

from . import common

TRAINLIST = "https://www.enoden-location.jp/Home/TrainList"
REFERER = "https://www.enoden-location.jp/Home/Index?mode=1"

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


def _get(url, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Referer": REFERER})
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
        raw = src.offline.root.joinpath("enoden-trainlist.json").read_text(
            encoding="utf-8")
    else:
        raw = _get(f"{TRAINLIST}?mode=1")
    rows = json.loads(raw)
    body = []
    # NOTE: rows[0] is the {"adjustment": bool} banner; vehicle rows follow.
    # No delay key exists in the vehicle objects -> nothing to emit. See
    # module docstring blocker. Kept as an explicit, audited empty feed.
    _ = rows  # parsed but deliberately not converted into delayed entries
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "www.enoden-location.jp",
    }
