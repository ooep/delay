"""IGR Iwate Galaxy Railway (ＩＧＲいわて銀河鉄道) live feed.

The operator's realtime map is a JORUDAN-hosted SPA at
https://igr.jorudan.biz/ja/ whose loadjson.min.js pulls:

    GET https://igr.jorudan.biz/contents/realtime_info.json
    GET https://igr.jorudan.biz/contents/oshirase.json

Shape (verified 2026-10-28 10:12 JST, in service):
    {"errors": [...],
     "filedate": {"ja": "...", "en": "...", ...},
     "datas": [ {disp_no, disp_name: "盛岡", direction: 0|1, div_id,
                 datas: [ {line_no, rec_datetime, win_no,
                           dely_time_min: <int minutes>,
                           timetable: {line_no,
                                       current_st_ml: {ja,en,cn,tw,ko},
                                       dely_time_str: {ja,...},
                                       dely_time: <int minutes>,
                                       line_kubun_ml: {ja: "普通", ...},
                                       shuten_ml: {ja: "金田一温泉行", ...},
                                       stations: [...]} } ] } ]}

Realtime fields live under "timetable" as multi-lang dicts; direction sits on
the line group, not the train record. Delay is whole minutes; we emit seconds
= dely_time * 60, filtered to >= 60 s. direction 1 = 上り (盛岡方面),
0 = 下り (目時・青い森方面) per the frontend's arrow-icon array order.
Out-of-service hours leave datas=[] — empty body is then the honest answer.
"""

import json
import urllib.request

from . import common

RT_URL = "https://igr.jorudan.biz/contents/realtime_info.json"

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

DELAY_THRESHOLD_SEC = 60


def _ja(v):
    """JORUDAN multi-lang object {ja,en,cn,tw,ko} — or a plain string."""
    if isinstance(v, dict):
        return v.get("ja") or v.get("en") or ""
    return v or ""


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
        grp_dir = group.get("direction")
        for tr in group.get("datas") or []:
            if not isinstance(tr, dict):
                continue
            # Live shape: realtime fields nested under "timetable" as
            # multi-lang dicts; keep a flat fallback for the old shape.
            tbl = tr.get("timetable") if isinstance(tr.get("timetable"), dict) else tr
            raw_delay = tbl.get("dely_time")
            if raw_delay is None:
                raw_delay = tr.get("dely_time_min")
            delay_min = int(raw_delay or 0)
            delay_sec = delay_min * 60
            if delay_sec < DELAY_THRESHOLD_SEC:
                continue
            cur = _ja(tbl.get("current_st_ml"))
            if not cur:
                continue
            line_no = tr.get("line_no") or tbl.get("line_no")
            direction = grp_dir if grp_dir is not None else tr.get("direction")
            train_type = _ja(tbl.get("line_kubun_ml")) or "普通"
            body.append({
                "owl:sameAs": f"IGR:{line_no}:{cur}",
                "odpt:railway": "odpt.Railway:IwateGalaxy.IwateGalaxy",
                "odpt:trainNumber": str(line_no or ""),
                "odpt:delay": delay_sec,
                "odpt:trainType": train_type,
                "_line_ja": "いわて銀河鉄道線",
                "_suki_line": "いわて銀河鉄道線",
                "_station": cur,
                "_dest": _ja(tbl.get("shuten_ml")) or None,
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
