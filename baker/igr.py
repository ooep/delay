"""IGR Iwate Galaxy Railway (ＩＧＲいわて銀河鉄道) live feed.

The operator's realtime map is a JORUDAN-hosted SPA at
https://igr.jorudan.biz/ja/ whose loadjson.min.js pulls:

    GET https://igr.jorudan.biz/contents/realtime_info.json
    GET https://igr.jorudan.biz/contents/oshirase.json

Shape (observed 2026-10-28 01:26 JST, out of service):
    {"errors": [],
     "filedate": {"ja": "2026年10月28日 01時26分36秒", ...},
     "datas": []}

During service, datas[] is a list of line groups, each:
    {line_kubun_ml: "青い森鉄道線 (目時)",
     datas: [ {line_no, direction: 0|1,          # 0=down arrow, 1=up arrow
               dely_time_min: <int minutes>,     # yellow >=1, red >=30
               dely_time, dely_time_str,
               current_st_ml: "<現在位置 駅名>",
               shuten_ml: "<終着 駅名>",
               dep_time, arr_time, is_passed, div_id, png} ]}

Delay is whole minutes; we emit seconds = dely_time_min * 60, filtered to
>= 60 s. direction 1 = 上り (盛岡方面), 0 = 下り (目時・青い森方面) per the
frontend's arrow-icon array order. Out-of-service hours leave datas=[] —
empty body is then the honest answer.
"""

import json
import urllib.request

from . import common

RT_URL = "https://igr.jorudan.biz/contents/realtime_info.json"

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

DELAY_THRESHOLD_SEC = 60


def _get_json(url, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Referer": "https://igr.jorudan.biz/ja/",
            })
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as exc:
            last = exc
            if attempt < retries:
                import time
                time.sleep(1.5)
    raise last


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        payload = src.offline_json("igr-realtime.json")
    else:
        try:
            payload = _get_json(RT_URL)
        except Exception as exc:
            print(f"igr: {type(exc).__name__}: {str(exc)[:120]}")
            return {"ok": False, "fetchedAt": common.jst_now().isoformat(),
                    "body": [], "source": "igr.jorudan.biz"}

    body = []
    for group in payload.get("datas") or []:
        if not isinstance(group, dict):
            continue
        for tr in group.get("datas") or []:
            if not isinstance(tr, dict):
                continue
            delay_min = int(tr.get("dely_time_min") or 0)
            delay_sec = delay_min * 60
            if delay_sec < DELAY_THRESHOLD_SEC:
                continue
            cur = tr.get("current_st_ml")
            if not cur:
                continue
            direction = tr.get("direction")
            body.append({
                "owl:sameAs": f"IGR:{tr.get('line_no')}:{cur}",
                "odpt:railway": "odpt.Railway:IwateGalaxy.IwateGalaxy",
                "odpt:trainNumber": str(tr.get("line_no") or ""),
                "odpt:delay": delay_sec,
                "odpt:trainType": "普通",
                "_line_ja": "いわて銀河鉄道線",
                "_suki_line": "いわて銀河鉄道線",
                "_station": cur,
                "_dest": tr.get("shuten_ml"),
                # frontend arrow array: index 0 = arrow_down (下り),
                # index 1 = arrow_up (上り)
                "_direction": "上り" if direction == 1 else (
                    "下り" if direction == 0 else None),
            })
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "igr.jorudan.biz/contents",
    }
