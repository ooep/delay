"""Odakyu (小田急) live train feed via their WebView native API.

The trainlocation SPA (d2zwzqde6wo0xp.cloudfront.net) talks to its native
backend base d6oynijiy33tb.cloudfront.net with a public x-api-key (extracted
from the APK Secret.java / web bundle):

    GET /trains                                   all trains in transit
    GET /trains/schedules/stopsv2?train_code&up_down&direction&time
                                                  per-train remaining stop list

/trains record:
    {train_code:"0014", delay_min:0, up_down:"01"(=上り,"10"=下り),
     train_type:1, train_position:102, position_id:101,
     destination_name:"新　宿", direction_name:"新　宿",
     merge_split:null|...}

stopsv2 record (queried for each delayed train; time = JST HHMM):
    [{train_code, destination_name, schedules:[{
        station_code, station_name, halt_station(1=停车),
        arrive_time(表定秒), estimate_arrival_time(预测秒),
        estimate_delay}]}]
Returns only stops after the requested time (i.e. the remaining leg) -
schedules[0] is the next station, stops with halt_station=1 are where this
train actually stops. This gives us both the current position and the
stopping pattern without needing the position_id(1..320)->station map,
which lives in the static SVG background image and is not in the JS bundle.
"""

import json
from datetime import datetime

from . import common

BASE = "https://d6oynijiy33tb.cloudfront.net"
TRAINS_URL = BASE + "/trains"
STOPS_URL = BASE + "/trains/schedules/stopsv2"
API_KEY = "8r7ngDW81q3qCNFVk59KB4bnazimt6TfpDOF4mB5"
HEADERS = {"x-api-key": API_KEY, "User-Agent": "Mozilla/5.0"}

DELAY_THRESHOLD_MIN = 1  # only emit delay >= 60 s

# train_type id -> 種別名称（来源 JS bundle 的图片键与停站表推断）
TRAIN_TYPE = {1: "各駅停車", 2: "快速", 3: "通勤急行", 7: "急行", 8: "特急ロマンスカー"}

# destination_name -> directionCode（来源 JS Cb 表：1=新宿/上り, 2=小田原/下り,
# 3=唐木田/下り, 4=片瀬江ノ島/下り, 5=新百合ヶ丘/上り, 6=相模大野/上り）。
# 区间车终点是沿线各站，按所属线路归一到方向：
#   下り本线(小田原方面)：本厚木/厚木/海老名/町田/相模大野/新松田/秦野/
#                         伊勢原/成城/登戸/向ヶ丘/読売ランド前... -> 2
#   下り多摩线：唐木田/新百合ヶ丘 -> 3
#   下り江之岛线：片瀬江ノ島/藤沢/大和 -> 4
#   上り区间终点：相模大野 -> 6、新百合ヶ丘 -> 5、其余(新宿方面) -> 1
def _direction_code(dest, ud):
    d = (dest or "").replace("　", "")
    if ud == "01":  # 上り
        if "相模大野" in d:
            return 6
        if "新百合" in d:
            return 5
        return 1  # 新宿及其他上行终点默认新宿方向
    # 下り
    if "唐木田" in d:
        return 3
    if "江ノ島" in d or "藤沢" in d or "大和" in d:
        return 4
    return 2  # 本线小田原方面（含区间终点本厚木/町田/成城等）


def _secs_hhmm(secs):
    """unix-seconds-from-midnight -> 'HH:MM'（小田急时刻口径）"""
    try:
        secs = int(secs)
        return f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}"
    except Exception:
        return None


def _stops_for(src, number, ud, dest, now_hhmm):
    """Fetch the remaining-stop leg for one delayed train. Returns
    (_station, _sched) or (None, None) on any failure."""
    dcode = _direction_code(dest, ud)
    if not number or not ud or not dcode:
        return None, None
    url = (
        f"{STOPS_URL}?train_code={number}&up_down={ud}"
        f"&direction={dcode}&time={now_hhmm}"
    )
    if src.offline:
        try:
            data = src.offline_json(f"odakyu-stops-{number}.json")
        except Exception:
            return None, None
    else:
        try:
            data = common.get_json(url, HEADERS)
        except Exception as exc:
            print(f"odakyu stops {number}: {type(exc).__name__}: {str(exc)[:100]}")
            return None, None
    if not isinstance(data, list) or not data:
        return None, None
    leg = data[0]
    scheds = leg.get("schedules") or []
    if not scheds:
        return None, None
    halts = [s for s in scheds if s.get("halt_station") == 1]
    if not halts:
        halts = scheds
    station_names = [s.get("station_name") for s in halts if s.get("station_name")]
    first, last = scheds[0], scheds[-1]
    sched = {
        "dep": _secs_hhmm(first.get("arrive_time")),
        "arr": _secs_hhmm(last.get("arrive_time")),
        "origin": first.get("station_name"),
        "destination": (leg.get("destination_name") or "").replace("　", ""),
        "stops": station_names,
    }
    return first.get("station_name"), sched


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

    now_hhmm = common.jst_now().strftime("%H%M")

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
        # 逐车补：下一站 + 剩余停站表（失败则降级为 v1 裸记录）
        station, sched = _stops_for(src, number, ud, dest, now_hhmm)
        rec = {
            "owl:sameAs": f"Odakyu:{number}:{pid}",
            "odpt:railway": "odpt.Railway:Odakyu.OdawaraLine",
            "odpt:trainNumber": number or None,
            "odpt:delay": delay_min * 60,
            "odpt:trainType": TRAIN_TYPE.get(t.get("train_type")),
            "_line_ja": "小田急",
            "_suki_line": "小田急線",
            "_station": station,
            "_dest": dest or None,
            "_direction": "上り" if ud == "01" else "下り",
            "_number_prefix": number or None,
        }
        if sched:
            rec["_sched"] = sched
        body.append(rec)
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "d6oynijiy33tb.cloudfront.net",
    }
