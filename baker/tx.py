"""Tsukuba Express (つくばエクスプレス, TX) live train feed.

Source: external-data.tx-app.com - the TX app's public position JSON, no key:
    https://external-data.tx-app.com/tid/trains.json?<ms>

Shape: {"updated_at": <unix seconds>, "trains":[{
    station_id, next_station_id, position, direction ("up"/"down"),
    train_kind_id (1..15), delay (MINUTES), train_number, train_length_id,
    destination_station_id, train_orchestration_number}]}

Line is fixed: MIR.TsukubaExpress / つくばエクスプレス (already in lines.json).
Station ids 11=秋葉原 .. 30=つくば (verified).
"""

from . import common

BASE = "https://external-data.tx-app.com"
TRAINS_URL = BASE + "/tid/trains.json?"

DELAY_THRESHOLD_MIN = 1

# station_id -> station name (verified against the TX app position list)
STATIONS = {
    11: "秋葉原", 12: "新御徒町", 13: "浅草", 14: "南千住", 15: "北千住",
    16: "青井", 17: "六町", 18: "八潮", 19: "三郷中央", 20: "南流山",
    21: "流山セントラルパーク", 22: "流山おおたかの森", 23: "柏の葉キャンパス",
    24: "柏たなか", 25: "守谷", 26: "みらい平", 27: "みどりの",
    28: "万博記念公園", 29: "研究学園", 30: "つくば",
}

# train_kind_id -> display type (per reverse-engineering of the TX app list)
TRAIN_KIND = {
    1: "普通", 2: "区間快速A", 3: "区間快速B", 4: "快速B", 5: "快速A",
    6: "新快速", 7: "特別快速B", 8: "特別快速A", 9: "団体", 10: "回送1",
    11: "回送2", 12: "試運転", 13: "通勤快速A", 14: "通勤快速B", 15: "通勤快速C",
}

# terminal station ids -> destination label for display
TERMINAL = {11: "秋葉原", 30: "つくば"}


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        try:
            data = src.offline_json("tx-trains.json")
        except Exception:
            return None
    else:
        ms = common.jst_now().strftime("%s") + "000"
        try:
            data = common.get_json(TRAINS_URL + ms)
        except Exception as exc:
            print(f"tx feed: {type(exc).__name__}: {str(exc)[:120]}")
            return None
    if not isinstance(data, dict):
        return None
    trains = data.get("trains")
    if not isinstance(trains, list):
        return None

    # updated_at (unix seconds) -> JST iso
    try:
        from datetime import datetime

        fetched_at = datetime.fromtimestamp(
            int(data["updated_at"]), tz=common.JST).isoformat()
    except Exception:
        fetched_at = common.jst_now().isoformat()

    body = []
    for t in trains:
        if not isinstance(t, dict):
            continue
        try:
            delay_min = int(t.get("delay") or 0)
        except Exception:
            delay_min = 0
        if delay_min < DELAY_THRESHOLD_MIN:
            continue
        sid = t.get("station_id")
        nxt = t.get("next_station_id")
        # report the next station the train is approaching (fallback: current)
        station = STATIONS.get(nxt) or STATIONS.get(sid)
        dest_id = t.get("destination_station_id")
        number = str(t.get("train_number") or "").strip()
        direction = t.get("direction")
        body.append({
            "owl:sameAs": f"TX:{number}:{sid}",
            "odpt:railway": "odpt.Railway:MIR.TsukubaExpress",
            "odpt:trainNumber": number or None,
            "odpt:delay": delay_min * 60,
            "odpt:trainType": TRAIN_KIND.get(t.get("train_kind_id")),
            "_line_ja": "つくばエクスプレス線",
            "_suki_line": "つくばエクスプレス線",
            "_station": station,
            "_dest": TERMINAL.get(dest_id, STATIONS.get(dest_id)),
            "_direction": "上り" if direction == "up" else "下り",
            "_number_prefix": number or None,
        })
    return {
        "ok": True,
        "fetchedAt": fetched_at,
        "body": body,
        "source": "external-data.tx-app.com",
    }
