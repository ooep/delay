"""Hankyu (阪急電鉄) live train feed for the baker.

Reverse-engineered 2026-10-27 from the 阪急沿線アプリ APK
(jp.co.hankyu.tokkapp v10.3.3). Keyless: a static x-auth-token is embedded
in the app's BuildConfig (jp.co.hankyu.data.BuildConfig.APP_AUTH_TOKEN).

    GET https://cdn.hankyu-app.com/v1/train/positions/?train_position_line_id={id}
    GET https://cdn.hankyu-app.com/v1/station/all/     (line/station master)

positions[] per train: {operation_number (the real train number),
  destination (text), from_station/_name .. to_station/_name (current
  track interval), line_destination_id, delay (minutes),
  is_display_delay, train_kind_key/_name, direction (0=up,1=down),
  track_number, is_edge_station}.

Unlike the Hanshin slot boards, real train numbers are published, so the
number-anchored leg match is used first; the from_station interval doubles
as the position anchor for the position-time fallback. Emit only delayed
trains (delay >= 1 min). 能勢電鉄 (妙見/日生) has no train_position_line_id
of its own (station master returns None), so it is not in this feed.
"""

from . import common

BASE = "https://cdn.hankyu-app.com/v1"
POSITIONS_URL = BASE + "/train/positions/"
STATION_ALL_URL = BASE + "/station/all/"

# Static app token (BuildConfig.APP_AUTH_TOKEN, release flavor).
AUTH_TOKEN = "709f682e261a6f57612a78f6b7f387d9e28082275cb2305bf7a0371ec22e8ed8"

HEADERS = {"Accept": "application/json", "x-auth-token": AUTH_TOKEN}

DELAY_THRESHOLD_MIN = 1

# train_position_line_id (from the app's station/{id}/lines/ master, probed
# 2026-10-27) -> (suki pool line, display line, dokorail railways hint).
# 今津線 exposes two ids (4/5, one per direction); 能勢電鉄 carries no
# train_position_line_id of its own (all None), so it is not in this feed.
# 神戸本線 through services run into 神戸高速線 (新開地/高速神戸), so the
# KobeRapid railways join the hint pool for leg resolution.
LINES = {
    1: ("神戸本線", "阪急電鉄神戸本線", ["Hankyu.KobeMain", "KobeRapid.HankyuBranch", "KobeRapid.TransitRailwayTozai"]),
    2: ("宝塚本線", "阪急電鉄宝塚本線", ["Hankyu.TakarazukaMain"]),
    3: ("阪急京都本線", "阪急電鉄京都本線", ["Hankyu.KyotoMain"]),
    4: ("今津線", "阪急電鉄今津線", ["Hankyu.Imazu"]),
    5: ("今津線", "阪急電鉄今津線", ["Hankyu.Imazu"]),
    6: ("伊丹線", "阪急電鉄伊丹線", ["Hankyu.Itami"]),
    7: ("甲陽線", "阪急電鉄甲陽線", ["Hankyu.Koyo"]),
    8: ("箕面線", "阪急電鉄箕面線", ["Hankyu.Minoo"]),
    9: ("千里線", "阪急電鉄千里線", ["Hankyu.Senri"]),
    10: ("阪急嵐山線", "阪急電鉄嵐山線", ["Hankyu.Arashiyama"]),
}


def _offline_json(src, name):
    try:
        return src.offline_json(name)
    except Exception:
        return None


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        station_all = _offline_json(src, "hk-station-all.json") or {}
        line_payloads = {}
        for lid in LINES:
            d = _offline_json(src, f"hk-positions/{lid}.json")
            if isinstance(d, dict):
                line_payloads[lid] = d
    else:
        try:
            station_all = common.get_json(STATION_ALL_URL, headers=HEADERS) or {}
        except Exception as exc:
            print(f"hankyu master: {type(exc).__name__}: {str(exc)[:100]}")
            return None
        line_payloads = {}
        for lid in LINES:
            try:
                d = common.get_json(
                    POSITIONS_URL + "?train_position_line_id=" + str(lid),
                    headers=HEADERS,
                )
            except Exception:
                continue
            if isinstance(d, dict):
                line_payloads[lid] = d

    # station name map (all lines in one payload)
    stations = {}
    for ln in station_all.get("lines") or []:
        for s in ln.get("stations") or []:
            stations[s.get("id")] = s.get("name")

    body = []
    seen = set()
    for lid, (suki_line, line_ja, railways) in LINES.items():
        d = line_payloads.get(lid)
        if not d:
            continue
        for p in d.get("positions") or []:
            if not isinstance(p, dict):
                continue
            try:
                delay_min = int(p.get("delay") or 0)
            except (TypeError, ValueError):
                delay_min = 0
            if delay_min < DELAY_THRESHOLD_MIN:
                continue
            op = p.get("operation_number")
            if op is None:
                continue
            # the same physical train may surface on two line ids (今津線
            # 4/5); emit each line's record once per train
            sig = (str(op), suki_line)
            if sig in seen:
                continue
            seen.add(sig)
            dest = p.get("destination")
            frm = p.get("from_station_name") or stations.get(p.get("from_station"))
            to = p.get("to_station_name") or stations.get(p.get("to_station"))
            kind = p.get("train_kind_name") or p.get("train_kind_key")
            body.append(
                {
                    "owl:sameAs": f"Hankyu:{lid}.{op}",
                    "odpt:railway": "odpt:Railway:Hankyu",
                    "odpt:trainNumber": str(op),
                    "odpt:delay": delay_min * 60,
                    "odpt:trainType": kind,
                    "_railways": railways,
                    "_line_ja": line_ja,
                    "_suki_line": suki_line,
                    # position anchor: the track interval's entry station
                    "_station": frm,
                    "_upcoming": [to] if to else [],
                    "_dest": dest,
                }
            )
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "cdn.hankyu-app.com/v1/train/positions",
    }
