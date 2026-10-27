"""Shared core for the VISH "buscatch rt3" live-position system.

Reverse-engineered 2026-10-27. The とやまロケーションシステム
(toyama.vtfm.jp) renders its map with the VISH buscatch rt3 backend
(buscatch.jp/rt3/toyama_map.php). Two stateless POST endpoints carry
everything we need, no session cookie required:

  POST /rt3/toyama_map.ajax.php   command=get_unko_list
      form: id=<id_name>, rosen_group_id, keito_id, rosen_id
      -> {"unko_list":[vehicle...], "teiryujo_list":[stop...], "route_list":...}

  POST /rt3/unko.ajax.php         command=get_unko_rosen_info
      form: id=<id_name>, rosen_id, unko_no=0, unko_yotei_id=<vehicle.id>
      -> per-vehicle detail:
           delay_minute          official delay in whole minutes (0 = on time)
           trip_headsign         destination (行先)
           start_teiryujo_name   origin stop, start_time = its HH:MM departure
           passed_teiryujo_name  last stop passed ("N を通過")
           jikokuhyo_list        full scheduled stop sequence for THIS train,
                                 each {teiryujo_id, jikokuhyo_time:"HH:MM",
                                 passed:1, current:1}

id_name values in the Toyama backend:
  t-bus           万葉線 (rosen_group_id 1971)
  chitetsu_train  富山地方鉄道 鉄道線 (rosen_group_id 2235)
  chitetsu_tram   富山地方鉄道 市内電車 (rosen_group_id 2167)

We emit ONLY vehicles whose delay_minute >= 1 (>= 60 s). No train number,
no train type is exposed, so odpt:trainNumber / odpt:trainType stay None
and the suki-direct matcher anchors on _official (this train's own
scheduled stop sequence + departure minute), exactly like baker/meitetsu.py.
"""

import json
import time
import urllib.parse
import urllib.request

from . import common

AJAX_LIST = "https://buscatch.jp/rt3/toyama_map.ajax.php"
AJAX_DETAIL = "https://buscatch.jp/rt3/unko.ajax.php"
REFERER_LIST = "https://buscatch.jp/rt3/toyama_map.php"
REFERER_DETAIL = "https://buscatch.jp/rt3/unko_rosen.php"

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

DELAY_THRESHOLD_MIN = 1


def _post(url, form, referer, retries=2):
    body = urllib.parse.urlencode(form).encode("utf-8")
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"User-Agent": UA, "Referer": referer,
                         "Content-Type": "application/x-www-form-urlencoded"},
                method="POST")
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as exc:
            last = exc
            if attempt < retries:
                time.sleep(1.5)
    raise last


def _hhm_to_min(v):
    """'23:30' -> 1410; None/'' -> None."""
    if not v:
        return None
    try:
        hh, mm = str(v).split(":")[:2]
        return int(hh) * 60 + int(mm)
    except ValueError:
        return None


def fetch_feed(id_name, rosen_group_id, lines, railway_prefix,
               source_domain, src=None):
    """Drive one operator group through the rosen list.

    lines: list of dicts {keito_id, rosen_id, keito_name, direction}.
    railway_prefix: e.g. "odpt.Railway:Manyo" / "odpt.Railway:ToyamaChihou".
    """
    src = src or common.Source()
    body = []
    seen = set()
    for ln in lines:
        keito_id, rosen_id = ln["keito_id"], ln["rosen_id"]
        try:
            if src.offline:
                lst = json.loads(src.offline.root.joinpath(
                    f"bc-{id_name}-{rosen_id}.json").read_text(
                        encoding="utf-8"))
            else:
                lst = _post(AJAX_LIST, {
                    "id": id_name, "command": "get_unko_list",
                    "rosen_group_id": rosen_group_id,
                    "keito_id": keito_id, "rosen_id": rosen_id},
                    REFERER_LIST)
        except Exception as exc:
            print(f"buscatch {id_name} list {rosen_id}: "
                  f"{type(exc).__name__}: {str(exc)[:100]}")
            continue
        tei_names = {t["id"]: t["name"]
                     for t in (lst.get("teiryujo_list") or []) if t.get("id")}
        vehicles = lst.get("unko_list") or []
        for veh in vehicles:
            yotei_id = veh.get("id")
            try:
                if src.offline:
                    dtl = json.loads(src.offline.root.joinpath(
                        f"bc-{id_name}-{rosen_id}-{yotei_id}.json").read_text(
                            encoding="utf-8"))
                else:
                    dtl = _post(AJAX_DETAIL, {
                        "id": id_name, "command": "get_unko_rosen_info",
                        "rosen_id": rosen_id, "unko_no": 0,
                        "unko_yotei_id": yotei_id}, REFERER_DETAIL)
            except Exception as exc:
                print(f"buscatch {id_name} detail {yotei_id}: "
                      f"{type(exc).__name__}: {str(exc)[:100]}")
                continue
            delay_min = int(dtl.get("delay_minute") or 0)
            if delay_min < DELAY_THRESHOLD_MIN:
                continue
            # Current stop: the flagged "current" row, else last passed stop.
            seq = dtl.get("jikokuhyo_list") or []
            current_row = next((s for s in seq if s.get("current")), None)
            station = None
            if current_row:
                station = tei_names.get(current_row.get("teiryujo_id"))
            if not station:
                passed = dtl.get("passed_teiryujo_name") or ""
                station = passed.replace("を通過", "").strip() or None
            # Scheduled stop sequence for THIS train (rows that actually
            # carry a departure time; short-turn trains have null rows at
            # the line tail).
            stops = [(s.get("teiryujo_id"), s.get("jikokuhyo_time"))
                     for s in seq if s.get("jikokuhyo_time")]
            stop_names = [tei_names.get(tid) or f"#{tid}" for tid, _ in stops]
            first_dep = _hhm_to_min(stops[0][1]) if stops else None
            terminal = dtl.get("trip_headsign") or veh.get("headsign")
            key = (rosen_id, yotei_id)
            if key in seen:
                continue
            seen.add(key)
            off = None
            if stop_names and first_dep is not None:
                off = {"departure": first_dep, "stations": stop_names,
                       "terminal": terminal}
            body.append({
                "owl:sameAs": f"Buscatch:{id_name}:{rosen_id}:{yotei_id}",
                "odpt:railway": f"{railway_prefix}.{ln['keito_name']}",
                "odpt:trainNumber": None,
                "odpt:delay": delay_min * 60,
                "odpt:trainType": None,
                "_line_ja": ln["keito_name"],
                "_suki_line": ln["keito_name"],
                "_station": station,
                "_dest": terminal,
                "_direction": ln.get("direction"),
                "_official": off,
            })
    return body
