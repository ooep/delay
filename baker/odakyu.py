"""Odakyu (小田急) live train feed via their WebView native API.

The trainlocation SPA (d2zwzqde6wo0xp.cloudfront.net) talks to its native
backend base d6oynijiy33tb.cloudfront.net with a public x-api-key (extracted
from the APK Secret.java / web bundle):

    GET /trains  -> all trains in transit (70+ rows, live)

/trains record:
    {train_code:"0014", delay_min:0, up_down:"01"(=上り,"10"=下り),
     train_type:1, train_position:102, position_id:101,
     destination_name:"新　宿", direction_name:"新　宿",
     merge_split:null|...}

position_id 1..320 是沿線位置点（前端 SVG 背景图坐标系，分三段
<=138 / <=247 / <=320）。位置点→站名映射表内嵌在静态地图背景图中，
JS 未明码，v1 不输出精确 _station，只输出番号/延误/行先/方向/种别，
足够 delays.py 按番号+行先消歧上板。后续拿到位置点表再补 _station。
"""

import json

from . import common

BASE = "https://d6oynijiy33tb.cloudfront.net"
TRAINS_URL = BASE + "/trains"
API_KEY = "8r7ngDW81q3qCNFVk59KB4bnazimt6TfpDOF4mB5"
HEADERS = {"x-api-key": API_KEY, "User-Agent": "Mozilla/5.0"}

DELAY_THRESHOLD_MIN = 1  # only emit delay >= 60 s

# train_type id -> 種別名称（来源 JS bundle 的图片键与停站表推断）
TRAIN_TYPE = {1: "各駅停車", 2: "快速", 3: "通勤急行", 7: "急行", 8: "特急ロマンスカー"}


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        try:
            data = src.offline_json("odakyu-trains.json")
        except Exception:
            return None
    else:
        try:
            data = common.get_json(TRAINS_URL, HEADERS)
        except Exception as exc:
            print(f"odakyu feed: {type(exc).__name__}: {str(exc)[:120]}")
            return None
    if not isinstance(data, dict):
        return None
    trains = data.get("trains")
    if not isinstance(trains, list):
        return None

    body = []
    for t in trains:
        if not isinstance(t, dict):
            continue
        try:
            delay_min = int(t.get("delay_min") or 0)
        except Exception:
            delay_min = 0
        if delay_min < DELAY_THRESHOLD_MIN:
            continue
        number = str(t.get("train_code") or "").strip()
        ud = t.get("up_down")
        # 目的地名内有全角空格（"新　宿"），与看板其他记录归一
        dest = (t.get("destination_name") or "").replace("　", "").strip()
        pid = t.get("position_id")
        body.append({
            "owl:sameAs": f"Odakyu:{number}:{pid}",
            "odpt:railway": "odpt.Railway:Odakyu.OdawaraLine",
            "odpt:trainNumber": number or None,
            "odpt:delay": delay_min * 60,
            "odpt:trainType": TRAIN_TYPE.get(t.get("train_type")),
            "_line_ja": "小田急",
            "_suki_line": "小田急線",
            "_station": None,  # 位置点→站名表待补（见模块 docstring）
            "_dest": dest or None,
            "_direction": "上り" if ud == "01" else "下り",
            "_number_prefix": number or None,
        })
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "d6oynijiy33tb.cloudfront.net",
    }
