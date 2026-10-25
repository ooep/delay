"""JR-East (東北/新潟/千葉/横浜エリア) live feed via どこトレ.

Source: https://doko-train.jp/sp/ - JR East's own regional train-position
service (plain JSON, no key, no bot protection observed):
    /json/trainstatus/lines_status.json        per-line aggregate (max delay)
    /json/trainstatus/<LINE_dokotrain_ID>.json per-line trains
    /json/line/dokotrain_line_master.json      line id -> name (78 groups)
    /json/station/dokotrain_station_master.json station code -> name

Train records: {TRAIN_LCLID: "1231M:<sta>-<end>.0", LATENCY: <minutes>,
STA_STATION/END_STATION/PRE_STATION/POS_STATION: <station codes>, ...}.
Numbers carry their JR-East suffix (1231M), so dokorail-bundle legs match
by exact number; the line bridge to dokorail railways happens generically
in delays.py via the line title (外房線 -> JR-East.Sotobo, ...).

This fills the JR-East realtime gap: dokorail's own JR-East feed answers
403 (its upstream), while doko-train publishes positions for 78 line
groups across 東北/新潟/千葉/横浜 - including lines dokorail lacks
(越後線, 仙山線, ...).
"""

from . import common

BASE = "https://doko-train.jp"
STATUS_URL = BASE + "/json/trainstatus/lines_status.json"
POS_URL = BASE + "/json/trainstatus/{}.json"
LINE_MASTER_URL = BASE + "/json/line/dokotrain_line_master.json"
STATION_MASTER_URL = BASE + "/json/station/dokotrain_station_master.json"

DELAY_THRESHOLD_MIN = 1


def _j(src, url, offline_name):
    if src.offline:
        try:
            return src.offline_json(offline_name)
        except Exception:
            return None
    try:
        return common.get_json(url, headers={"Referer": BASE + "/sp/"})
    except Exception as exc:
        print(f"dokotrain {offline_name or url}: {type(exc).__name__}: {str(exc)[:120]}")
        return None


def fetch(src=None):
    src = src or common.Source()
    status = _j(src, STATUS_URL, "dt-lines_status.json")
    if not status or not isinstance(status.get("LINE_STATUS"), dict):
        return None
    delayed = {
        lid: v
        for lid, v in status["LINE_STATUS"].items()
        if isinstance(v, dict) and v.get("MAX_DELAY_MINUTES", 0) >= DELAY_THRESHOLD_MIN
    }
    now = common.jst_now()
    if not delayed:
        return {"ok": True, "fetchedAt": now.isoformat(), "body": []}

    master = _j(src, LINE_MASTER_URL, "dt-dokotrain_line_master.json")
    line_names = {
        lid: meta.get("LINE_dokotrain_NAME", "")
        for lid, meta in ((master or {}).get("LINE") or {}).items()
    }
    stations = {}
    st = _j(src, STATION_MASTER_URL, "dt-dokotrain_station_master.json")
    for row in (st or {}).get("data") or []:
        code = row.get("STATION_ID")
        if code:
            stations[code] = row.get("STATION_NAME_KANJI")

    body = []
    for lid in delayed:
        pos = _j(src, POS_URL.format(lid), f"dt-{lid}.json")
        if not pos:
            continue
        line_name = common.PAREN_RE.sub("", line_names.get(lid, "")).strip() or f"dokotrain:{lid}"
        for key, t in ((pos.get("LINE_STATUS") or {}).get("TRAIN_STATUS") or {}).items():
            if not isinstance(t, dict):
                continue
            latency = t.get("LATENCY") or 0
            if latency < DELAY_THRESHOLD_MIN:
                continue
            number = str(t.get("TRAIN_LCLID") or key).split(":")[0]
            cur = str(t.get("CUR_STATION") or "0")
            at = cur if cur != "0" else (t.get("PRE_STATION") or "")
            body.append(
                {
                    "owl:sameAs": f"JR-East-dokotrain:{lid}.{number}",
                    "odpt:railway": "odpt.Railway:JR-East",
                    "odpt:trainNumber": number,
                    "odpt:delay": int(latency) * 60,
                    "_line_ja": line_name,
                    "_suki_line": line_name,
                    "_station": stations.get(at),
                    "_dokotrain_line": lid,
                }
            )
    return {
        "ok": True,
        "fetchedAt": now.isoformat(),
        "body": body,
        "source": "doko-train.jp",
    }
