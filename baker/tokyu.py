"""Tokyu (東急) live train feed via the Sotetsu-app shared data backend.

Two-stage, keyless (reverse-engineered from the Sotetsu app web client):

  stage1  GET https://04x5f9ykzc.execute-api.ap-northeast-1.amazonaws.com/prod/
              external-data-url?key={toyoko|meguro|shinyokohama}.json
          -> {"url": "<S3 presigned URL, X-Amz-Expires=300>"}

  stage2  GET <presigned url>
          -> {"trains":[{operation_number, kind("普/急/Ｆ"), train_kind,
                        delay_time(分), line_id, num_of_cars, train_line_id,
                        station_id, section_id, track_number, up(bool=上り),
                        train_number, destination_station_code}, ...],
              "delays": null}

The three keys cover the through-service corridor:
  toyoko=東横線(line_id 26001), meguro=目黒線(26002),
  shinyokohama=東急新横浜線(26009). A through train appears in more than one
  feed (e.g. a Meguro through train is listed under both toyoko and meguro);
  we dedup by train_number and keep the record whose line_id == train_line_id
  (the train's home-line view).

Station names: the payload carries only numeric codes. The code->name tables
below were mined from the Sotetsu frontend bundle (sotetsu-frontend.*.js):
  - station_id IS a Tokyu "KYId" (東急駅番), mapped to a name via the chain
    KYId -> Sotetsu internal station id -> name_ja (21 entries recovered);
  - when station_id is null (区间走行 on the Shin-yokohama link), section_id
    resolves to a KYId first;
  - destination_station_code resolves directly to a name (shared Sotetsu-app
    through-service destination table, 106 entries).

NOTE: dento.json / oimachi.json (大井町線・池上線・世田谷線など) live behind a
different API gateway (fp5owad3w3) that enforces an Origin header; out of
scope for this first drop (left as a comment, returns no data).
"""

from . import common

STAGE1 = (
    "https://04x5f9ykzc.execute-api.ap-northeast-1.amazonaws.com"
    "/prod/external-data-url"
)
# feed key -> native Tokyu line_id (the line this feed's trains are on)
FEED_KEYS = ("toyoko", "meguro", "shinyokohama")

# Tokyu line_id -> (dokorail railway suffix, ja title)
# 26001 東横線 / 26002 目黒線 / 26003 田園都市線 / 26009 東急新横浜線
LINE_ID_MAP = {
    26001: ("Tokyu.Toyoko", "東横線"),
    26002: ("Tokyu.Meguro", "目黒線"),
    26003: ("Tokyu.DenEnToshi", "田園都市線"),
    26009: ("Tokyu.TokyuShinYokohama", "東急新横浜線"),
}

# Tokyu KYId (payload station_id) -> station name.
# Recovered from sotetsu-frontend.*.js: KYId -> sotetsu station id -> name_ja.
KYID_TO_STATION = {
    24: "目黒",
    26: "渋谷",
    57: "武蔵小杉",
    908: "代官山",
    909: "中目黒",
    910: "祐天寺",
    911: "学芸大学",
    912: "都立大学",
    913: "自由が丘",
    914: "田園調布",
    915: "多摩川",
    916: "新丸子",
    918: "元住吉",
    919: "日吉",
    920: "綱島",
    921: "大倉山",
    922: "菊名",
    923: "妙蓮寺",
    924: "白楽",
    925: "東白楽",
    926: "反町",
    927: "横浜",
    929: "不動前",
    930: "武蔵小山",
    931: "西小山",
    932: "洗足",
    933: "大岡山",
    934: "奥沢",
    982: "新綱島",
    983: "新横浜",
}

# section_id -> KYId (used when payload station_id is null). The Shin-yokohama
# sections are the odd ones out; the 東横線 sections run 渋谷->横浜 twice, once
# per direction, offset by 21 (2->渋谷/3->代官山 ... and 24->代官山/25->中目黒
# ...), so the up run continues 14->日吉 ... 22->横浜 and the down run
# 36->綱島 ... 43->横浜. 923=妙蓮寺 was confirmed against the timetable (both
# directions land on a unique run at the reported minute), the rest follow the
# same +21 offset and the published 東急 station order.
SECTION_TO_KYID = {
    2: 26, 3: 908, 4: 909, 5: 910, 6: 911, 7: 912, 8: 913, 9: 914,
    10: 915, 11: 916, 12: 57, 13: 918, 14: 919, 15: 920, 16: 921, 17: 922,
    18: 923, 19: 924, 20: 925, 21: 926, 22: 927,
    24: 908, 25: 909, 26: 910, 27: 911, 28: 912, 29: 913, 30: 914,
    31: 915, 32: 916, 33: 57, 34: 918, 35: 919,
    36: 920, 37: 921, 38: 922, 39: 923, 40: 924, 41: 925, 42: 926, 43: 927,
    46: 24, 47: 929, 48: 930, 49: 931, 50: 932, 51: 933, 52: 934,
    53: 914, 54: 915, 55: 916, 56: 57, 57: 918,
    59: 929, 60: 930, 61: 931, 62: 932, 63: 933, 64: 934,
    65: 914, 66: 915, 67: 916, 68: 57, 69: 918, 70: 919,
    263: 919, 264: 982, 265: 982, 266: 983,
}

# destination_station_code -> name (shared Sotetsu-app through-service table).
DEST_CODE_TO_NAME = {
    2: "西武球場前", 3: "西武秩父", 4: "飯能", 5: "入間市", 6: "小手指",
    7: "西所沢", 8: "所沢", 9: "清瀬", 10: "保谷", 11: "石神井公園",
    12: "練馬高野台", 13: "練馬", 15: "西武線直通", 17: "小川町",
    18: "森林公園", 19: "坂戸", 20: "川越市", 21: "上福岡", 22: "志木",
    24: "東武東上線直通", 26: "和光市", 27: "地下鉄成増", 28: "小竹向原",
    29: "千川", 30: "池袋", 31: "新宿三丁目", 33: "副都心線直通",
    36: "北千住", 37: "南千住", 38: "八丁堀", 39: "霞ケ関", 40: "広尾",
    42: "日比谷線直通", 45: "西高島平", 46: "高島平", 47: "本蓮沼",
    48: "新板橋", 49: "巣鴨", 50: "水道橋", 51: "日比谷", 52: "芝公園",
    54: "三田線直通", 57: "浦和美園", 58: "東川口", 59: "鳩ヶ谷",
    60: "赤羽岩淵", 61: "王子神谷", 62: "駒込", 63: "市ケ谷",
    64: "溜池山王", 65: "白金高輪", 67: "南北線直通", 68: "南北線・三田線直通",
    71: "目黒", 72: "不動前", 73: "武蔵小山", 74: "西小山", 75: "洗足",
    76: "大岡山", 77: "奥沢", 78: "渋谷", 79: "代官山", 80: "中目黒",
    81: "祐天寺", 82: "学芸大学", 83: "都立大学", 84: "自由が丘",
    85: "田園調布", 86: "多摩川", 87: "新丸子", 88: "武蔵小杉",
    89: "元住吉", 90: "日吉", 91: "綱島", 92: "大倉山", 93: "菊名",
    94: "妙蓮寺", 95: "白楽", 96: "東白楽", 97: "反町", 98: "横浜",
    99: "新高島", 100: "みなとみらい", 101: "馬車道", 102: "日本大通り",
    103: "元町・中華街", 107: "新綱島", 108: "新横浜", 111: "相鉄線直通",
    112: "西谷", 113: "二俣川", 114: "瀬谷", 115: "大和", 116: "相模大塚",
    117: "かしわ台", 118: "海老名", 119: "いずみ野", 120: "湘南台",
    121: "羽沢横浜国大", 129: "蒲田方面", 130: "沼部", 131: "鵜の木",
    132: "下丸子", 133: "武蔵新田", 134: "矢口渡", 135: "蒲田",
}

# payload kind (車種) -> display type
KIND_MAP = {"普": "各停", "急": "急行", "通": "通勤", "Ｆ": "Fライナー"}


def _station_name(rec):
    """Resolve a station name from station_id (KYId) or, failing that, the
    section_id -> KYId chain. Returns None when the code is unknown."""
    sid = rec.get("station_id")
    if sid is not None:
        return KYID_TO_STATION.get(sid)
    sec = rec.get("section_id")
    if sec is not None:
        ky = SECTION_TO_KYID.get(sec)
        if ky is not None:
            return KYID_TO_STATION.get(ky)
    return None


def fetch(src=None):
    src = src or common.Source()
    merged = {}  # train_number -> record
    for key in FEED_KEYS:
        if src.offline:
            try:
                payload = src.offline_json(f"tokyu-{key}.json")
            except Exception:
                continue
        else:
            try:
                stage1 = common.get_json(f"{STAGE1}?key={key}.json")
                s3url = stage1.get("url")
                if not s3url:
                    continue
                payload = common.get_json(s3url)
            except Exception as exc:  # noqa: BLE001 - one feed must not abort
                print(f"tokyu {key}: {type(exc).__name__}: {str(exc)[:120]}")
                continue
        for rec in payload.get("trains") or []:
            if not isinstance(rec, dict):
                continue
            num = rec.get("train_number")
            if not num:
                continue
            # dedup: prefer the record whose line_id == train_line_id (the
            # train's home-line view over the through-runner view)
            prev = merged.get(num)
            if prev is None:
                merged[num] = rec
            elif rec.get("line_id") == rec.get("train_line_id"):
                merged[num] = rec

    body = []
    for rec in merged.values():
        delay_min = rec.get("delay_time") or 0
        try:
            delay_min = int(delay_min)
        except (TypeError, ValueError):
            continue
        delay_sec = delay_min * 60
        if delay_sec < 60:
            continue
        line_id = rec.get("line_id")
        railway, line_ja = LINE_ID_MAP.get(line_id, (None, None))
        number = str(rec.get("train_number") or "")
        op_no = rec.get("operation_number")
        dest = DEST_CODE_TO_NAME.get(rec.get("destination_station_code"))
        up = rec.get("up")
        body.append(
            {
                "owl:sameAs": f"Tokyu-tid:{line_id}.{number}",
                "odpt:railway": f"odpt.Railway:{railway}" if railway else "",
                "odpt:trainNumber": number,
                "odpt:delay": delay_sec,
                "odpt:trainType": KIND_MAP.get(rec.get("kind")),
                "odpt:carComposition": int(rec["num_of_cars"])
                if str(rec.get("num_of_cars") or "").isdigit()
                else None,
                "_line_ja": line_ja,
                "_suki_line": line_ja,
                "_station": _station_name(rec),
                "_dest": dest,
                "_direction": "up" if up else "down",
                # operation_number (e.g. 449) is the leading digit run of the
                # 8-digit train_number ("04491520") and what dokorail legs file
                # trains under ("4491M") -> prefix match.
                "_number_prefix": str(op_no) if op_no is not None else None,
            }
        )
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "sotetsuapp.com external-data (Tokyu tid)",
    }
