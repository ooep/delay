"""Seibu (西武) live train feed via the official trainfo-api backend.

Keyless Retrofit JSON (Accept-Language: ja is enough):

  GET https://train.seibuapp.jp/trainfo-api/ti/v1.0/trains?lineId={L001..L022}
  -> {"total": N, "offset": 0, "train": [TrainInfo, ...]}

TrainInfo (reverse-engineered): trainNo(列番), lineId, lineName,
trainType/trainName(種別/愛称), direction("up"/"down"),
fromStationId/fromStationName(当前区间起点站 -> _station),
toStationId/toStationName(下一站, often null),
destination/endStationName(行先 -> _dest), startStationName,
delay(分), numberOfCars, status, isOnRail, isPassing, sectionNo, note.

The gateway occasionally 502s the sandbox (transient); live() retries each line
a few times and a failing line just costs itself. GitHub Action egress is
expected to be more stable - flagged "待 Action 实测" for the first run.
"""

from . import common

BASE = "https://train.seibuapp.jp/trainfo-api/ti/v1.0/trains"

# Seibu L-code -> (dokorail railway suffix, ja title). Verified against the
# live lineName field and data/lines.json. L003 豊島線 / L010 西武園線 are
# bonus lines that also carry trains; L004/L006/L007/L014..L020 answer empty.
LINE_MAP = {
    "L001": ("Seibu.Ikebukuro", "池袋線"),
    "L002": ("Seibu.Sayama", "狭山線"),
    "L003": ("Seibu.Toshima", "豊島線"),
    "L005": ("Seibu.SeibuYurakucho", "西武有楽町線"),
    "L008": ("Seibu.SeibuChichibu", "西武秩父線"),
    "L009": ("Seibu.Shinjuku", "新宿線"),
    "L010": ("Seibu.Seibuen", "西武園線"),
    "L011": ("Seibu.Haijima", "拝島線"),
    "L012": ("Seibu.Kokubunji", "国分寺線"),
    "L013": ("Seibu.Tamako", "多摩湖線"),
    "L021": ("Seibu.Tamagawa", "多摩川線"),
    "L022": ("Seibu.Yamaguchi", "山口線"),
}

# trainType code -> display type. The API returns a numeric code; the names
# below are inferred from the per-train `note` field / stop patterns observed
# live (67 = note literally "快速急行"; 9 = 特急レッドアロー/ラビュー;
# 15 = 各駅停車; 4 = 急行 stop pattern; 8 = 西武線内のみ 普通; 1 = 回送).
# Anything unmapped stays None rather than guessed.
TYPE_MAP = {
    4: "急行",
    8: "普通",
    9: "特急",
    15: "各停",
    67: "快速急行",
}
SKIP_TYPES = {1}  # 回送 (deadhead) - not a passenger delay


def _fetch_line(src, lid):
    if src.offline:
        # offline replays the captured {Lcode: payload} bundle
        return None  # handled in fetch()
    url = f"{BASE}?lineId={lid}"
    try:
        return common.get_json(url, headers={"Accept-Language": "ja"})
    except Exception as exc:  # noqa: BLE001 - one line must not abort
        print(f"seibu {lid}: {type(exc).__name__}: {str(exc)[:120]}")
        return None


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        try:
            bundle = src.offline_json("seibu-trains.json")
        except Exception:
            return None
        payloads = {lid: bundle.get(lid) for lid in LINE_MAP}
    else:
        payloads = {}
        for lid in LINE_MAP:
            payloads[lid] = _fetch_line(src, lid)

    body = []
    for lid, payload in payloads.items():
        if not isinstance(payload, dict):
            continue
        railway, line_ja = LINE_MAP[lid]
        for t in payload.get("train") or []:
            if not isinstance(t, dict):
                continue
            try:
                delay_sec = int(t.get("delay") or 0) * 60
            except (TypeError, ValueError):
                continue
            if delay_sec < 60:
                continue
            tt = t.get("trainType")
            try:
                tt = int(tt)
            except (TypeError, ValueError):
                tt = None
            if tt in SKIP_TYPES:
                continue
            number = str(t.get("trainNo") or "")
            cars = t.get("numberOfCars")
            body.append(
                {
                    "owl:sameAs": f"Seibu:{lid}.{number}",
                    "odpt:railway": f"odpt.Railway:{railway}",
                    "odpt:trainNumber": number,
                    "odpt:delay": delay_sec,
                    "odpt:trainType": TYPE_MAP.get(tt),
                    "odpt:carComposition": int(cars)
                    if str(cars or "").isdigit()
                    else None,
                    # lineName from the API already matches lines.json ja
                    # (池袋線/新宿線/...); trust it, fall back to the map.
                    "_line_ja": t.get("lineName") or line_ja,
                    "_suki_line": line_ja,
                    "_station": t.get("fromStationName"),
                    # 始发站: 西武 app 明确给出本列车从哪一站开出。suki 里
                    # 同一条线有大量只差始发站的短交路 key(豊島園/石神井公園/
                    # 小手指…),仅靠过站分钟会把延误贴到别的列车上。
                    "_start": t.get("startStationName"),
                    "_dest": t.get("endStationName"),
                    "_direction": t.get("direction"),
                    "_number_prefix": number,
                }
            )
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "train.seibuapp.jp trainfo-api",
    }
