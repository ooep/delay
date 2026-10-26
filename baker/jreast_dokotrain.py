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

import concurrent.futures

from . import common

BASE = "https://doko-train.jp"
STATUS_URL = BASE + "/json/trainstatus/lines_status.json"
POS_URL = BASE + "/json/trainstatus/{}.json"
DIAGRAM_URL = BASE + "/json/diagram/line/{}.json"
LINE_MASTER_URL = BASE + "/json/line/dokotrain_line_master.json"
STATION_MASTER_URL = BASE + "/json/station/dokotrain_station_master.json"

DELAY_THRESHOLD_MIN = 1

# mini-Shinkansen (山形 902Y / 秋田 110A) run on physical 本線 tracks and the
# suki dataset labels them by the physical line, not the Shinkansen brand
SUKI_LINE_REWRITES = {
    "山形新幹線": "奥羽本線",
    "秋田新幹線": "田沢湖線",
}


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
    source_time = status.get("announced_date")
    if not delayed:
        return {"ok": True, "fetchedAt": now.isoformat(), "source_time": source_time, "body": []}

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
    pos_stamps = []
    # doko-train has no bot protection (verified: bare curl / plain urllib
    # both answer 200), so the per-line fetch is parallelized to cut the
    # serial walk (~40 lines * 2 requests each) down to seconds.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:

        def _line(lid):
            pos = _j(src, POS_URL.format(lid), f"dt-{lid}.json")
            if not pos:
                return [], []
            stamp = [pos["announced_date"]] if pos.get("announced_date") else []
            line_name = common.PAREN_RE.sub("", line_names.get(lid, "")).strip() or f"dokotrain:{lid}"
            # doko-train ships the authoritative timetable for its own lines
            # (incl. BRT and composite groups dokorail lacks) - use it as the
            # stop-pattern source, same as JR-Hokkaido's daiya files
            diagram = _j(src, DIAGRAM_URL.format(lid), f"dt-diag-{lid}.json")
            dia = {}
            for run in (diagram or {}).get("DIAGRAM") or []:
                if isinstance(run, dict) and run.get("TRAIN_ID"):
                    dia[str(run["TRAIN_ID"])] = run
            entries = []
            for key, t in ((pos.get("LINE_STATUS") or {}).get("TRAIN_STATUS") or {}).items():
                if not isinstance(t, dict):
                    continue
                latency = t.get("LATENCY") or 0
                if latency < DELAY_THRESHOLD_MIN:
                    continue
                number = str(t.get("TRAIN_LCLID") or key).split(":")[0]
                cur = str(t.get("CUR_STATION") or "0")
                at = cur if cur != "0" else (t.get("PRE_STATION") or "")
                entry = {
                    "owl:sameAs": f"JR-East-dokotrain:{lid}.{number}",
                    "odpt:railway": "odpt.Railway:JR-East",
                    "odpt:trainNumber": number,
                    "odpt:delay": int(latency) * 60,
                    "_line_ja": line_name,
                    "_suki_line": SUKI_LINE_REWRITES.get(line_name, line_name),
                    "_station": stations.get(at),
                    "_dokotrain_line": lid,
                }
                # attach the diagram's full stop pattern when the run is known
                run = dia.get(number)
                if run and isinstance(run.get("SDL"), list) and len(run["SDL"]) >= 2:
                    stops = []
                    for s in run["SDL"]:
                        name = s.get("RLSTC_LNAME")
                        if not name:
                            continue
                        stops.append(
                            {
                                "station": name,
                                "arr": s.get("STA") or None,
                                "dep": s.get("STD") or s.get("STA"),
                            }
                        )
                    if len(stops) >= 2:
                        entry["_sched"] = {
                            "origin": stops[0]["station"],
                            "destination": stops[-1]["station"],
                            "dep": stops[0]["dep"],
                            "arr": stops[-1].get("arr") or stops[-1]["dep"],
                            "stops": [s["station"] for s in stops],
                            "_stops_full": stops,
                        }
                entries.append(entry)
            return entries, stamp

        for entries, stamp in ex.map(_line, delayed):
            body += entries
            pos_stamps += stamp
    stamps = [source_time] + pos_stamps
    return {
        "ok": True,
        "fetchedAt": now.isoformat(),
        "source_time": max((s for s in stamps if s), default=None) or None,
        "body": body,
        "source": "doko-train.jp",
    }
