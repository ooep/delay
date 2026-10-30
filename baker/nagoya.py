"""名古屋市交通局 — Nagoya Municipal Subway line-level operation status.

Source (official, keyless): the subway SPA polls
    https://www.kotsu.city.nagoya.jp/datas/latest_traffic.json
-> one row per 運行系統 with {rosen_id, icon_cd, traffic_title,
traffic_message, traffic_section, create_datetime}. icon_cd: N_T_ICO normal,
W_T_ICO delay, C_T_ICO caution, E_T_ICO suspended. B_LINE is the city-bus
board, not a railway.

Nagoya publishes no per-train delay numbers, so a non-normal line becomes a
line-level entry at the 5-minute floor with the official text attached — the
same convention as baker/tokyometro_unkou.py and baker/osakametro.py. All
lines normal -> empty body (the board shows nothing for this operator).

The operator groups 名城線 and 名港線 into one M_LINE status row (they are
through-operated), while data/lines.json keeps them as two railways; that row
therefore emits one entry per railway so both names resolve in the board.

The JSON file starts with a UTF-8 BOM; decode with utf-8-sig. A fetch failure
returns ok=False so a dead source never reads as "all on time".
"""

import json
import os

from . import common

BASE = os.environ.get("NAGOYA_BASE", "").strip() or "https://www.kotsu.city.nagoya.jp"
URL = BASE + "/datas/latest_traffic.json"

LINE_LEVEL_DELAY_SECONDS = 5 * 60

# rosen_id -> [(lines.json railway, 日本語線名)] — names must match
# data/lines.json, the board takes `ja` from there.
#   H 東山線 / M 名城線・名港線 / T 鶴舞線 / S 桜通線 / K 上飯田線
LINES = {
    "H_LINE": [("NagoyaMunicipal.Higashiyama", "名古屋市営1号線東山線")],
    "M_LINE": [
        ("NagoyaMunicipal.Meijo", "名古屋市営名城線"),
        ("NagoyaMunicipal.Meiko", "名古屋市営名港線"),
    ],
    "T_LINE": [("NagoyaMunicipal.Tsurumai", "名古屋市営3号線鶴舞線")],
    "S_LINE": [("NagoyaMunicipal.SakuraDori", "名古屋市営6号線桜通線")],
    "K_LINE": [("NagoyaMunicipal.Kamiiida", "名古屋市営上飯田線")],
}
STATUS = {"W_T_ICO": "delayed", "C_T_ICO": "caution", "E_T_ICO": "suspended"}


def _rows(src, fetched_at):
    if src.offline:
        try:
            return src.offline.get_json("nagoya-latest-traffic.json")
        except Exception:
            return None
    try:
        raw = common.http_get(
            URL + "?_=" + str(int(fetched_at.timestamp() * 1000)),
            headers={"Accept": "application/json"}, retries=2, timeout=15,
        )
        return json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:
        print(f"nagoya latest_traffic: {type(exc).__name__}: {str(exc)[:120]}")
        return None


def fetch(src=None):
    src = src or common.Source()
    fetched_at = common.jst_now()
    rows = _rows(src, fetched_at)
    if not isinstance(rows, list):
        return {"ok": False, "fetchedAt": fetched_at.isoformat(), "body": [],
                "source": "kotsu.city.nagoya.jp", "error": "latest_traffic unavailable"}

    body = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = STATUS.get(str(row.get("icon_cd") or ""))
        lines = LINES.get(str(row.get("rosen_id") or ""))
        if not status or not lines:
            continue
        text = " ".join(
            str(row.get(k) or "").strip()
            for k in ("traffic_title", "traffic_section", "traffic_message")
            if str(row.get(k) or "").strip()
        )
        for railway, ja in lines:
            body.append({
                "owl:sameAs": "NagoyaMunicipal:{}:{}".format(
                    row.get("traffic_no") or row.get("rosen_id"),
                    railway.rsplit(".", 1)[-1]),
                "odpt:railway": "odpt.Railway:" + railway,
                "odpt:trainNumber": None,
                "odpt:delay": LINE_LEVEL_DELAY_SECONDS,
                "odpt:trainType": None,
                "_line_ja": ja,
                "_metro_status": status,
                "_metro_status_text": text[:300],
                "_metro_updated": row.get("create_datetime"),
            })
    return {"ok": True, "body": body, "fetchedAt": fetched_at.isoformat(),
            "source": "kotsu.city.nagoya.jp"}
