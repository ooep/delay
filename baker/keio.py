"""Keio (京王電鉄) live train feed via the tid-platform web backend.

The Keio App's train-position WebView (i.opentidkeio.jp/html/zaisen_*.html)
is served by the same tid platform as Keisei's zaisen site. Reverse-engineered
2026-10-27 from libapp.so (Flutter AOT strings) + live page capture:

    GET https://i.opentidkeio.jp/data/traffic_info.json?ts=<ms>
        live trains, grouped by position point id.
    GET https://i.opentidkeio.jp/config/position.json   (static v2.20)
        position point dictionary: E###=駅, U###/D###=上り/下り駅間.
    GET https://i.opentidkeio.jp/config/syasyu.json    種别 code -> name
    GET https://i.opentidkeio.jp/config/ikisaki.json    行先 code -> name

traffic_info.json shape:
    {"up":[{"dt":[...],"st":"0"}],
     "TS":[{"id":"E002","sn":"K","ps":[
        {"tr":" 0085 ","sy":"1","dl":"00","ik_tr":"032","sr":"10",...}]}]}
  tr=列車番号, dl=遅れ分(2桁文字列), sy=種别code, ik_tr=行先code,
  sr=方面code (10=京王線下り, 5=井の頭線下り), id=position.json の点ID.
"""

import json

from . import common

BASE = "https://i.opentidkeio.jp"
TRAFFIC_URL = BASE + "/data/traffic_info.json"
CONFIG = {
    "position": BASE + "/config/position.json",
    "syasyu": BASE + "/config/syasyu.json",
    "ikisaki": BASE + "/config/ikisaki.json",
}

DELAY_THRESHOLD_MIN = 1  # only emit delay >= 60 s


def _load_config(src, key, url):
    if src.offline:
        try:
            return src.offline_json(f"keio-{key}.json")
        except Exception:
            return {}
    try:
        data = common.get_json(url + "?ver=2.20")
    except Exception as exc:
        print(f"keio config {key}: {type(exc).__name__}: {str(exc)[:100]}")
        return {}
    if key == "position":
        return {p["ID"]: p.get("name", p["ID"]) for p in data.get("pos", [])}
    if key == "syasyu":
        return {x["code"]: x.get("name") or None for x in data.get("syasyu", [])}
    if key == "ikisaki":
        return {x["code"]: x.get("name") for x in data.get("ikisaki", [])}
    return {}


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        try:
            data = src.offline_json("keio-traffic.json")
        except Exception:
            return None
    else:
        try:
            ms = common.jst_now().strftime("%s") + "000"
            data = common.get_json(TRAFFIC_URL + "?ts=" + ms)
        except Exception as exc:
            print(f"keio feed: {type(exc).__name__}: {str(exc)[:120]}")
            return None
    if not isinstance(data, dict):
        return None

    posmap = _load_config(src, "position", CONFIG["position"])
    syasyu = _load_config(src, "syasyu", CONFIG["syasyu"])
    ikisaki = _load_config(src, "ikisaki", CONFIG["ikisaki"])

    body = []
    for grp in data.get("TS") or []:
        pid = grp.get("id") or ""
        pname = posmap.get(pid, pid)
        # 井の頭線: E09x 台（富士見ヶ丘/三鷹台/吉祥寺方面）；其余=京王線
        inokashira = pid.startswith("E09") or pid.startswith("E10") or pid.startswith("E11")
        railway = "Keio.Inokashira" if inokashira else "Keio.KeioLine"
        line_ja = "井の頭線" if inokashira else "京王線"
        for t in grp.get("ps") or []:
            if not isinstance(t, dict):
                continue
            dl = t.get("dl")
            try:
                delay_min = int(dl) if str(dl).isdigit() else 0
            except Exception:
                delay_min = 0
            if delay_min < DELAY_THRESHOLD_MIN:
                continue
            number = str(t.get("tr") or "").strip()
            dest_code = str(t.get("ik_tr") or t.get("ik") or "").strip()
            dest = ikisaki.get(dest_code) or None
            # 上り判定: 行先为新宿/渋谷（都心方向）
            direction = "上り" if dest and ("新宿" in dest or dest == "渋谷") else "下り"
            body.append({
                "owl:sameAs": f"Keio:{number}:{pid}",
                "odpt:railway": f"odpt.Railway:{railway}",
                "odpt:trainNumber": number or None,
                "odpt:delay": delay_min * 60,
                "odpt:trainType": syasyu.get(str(t.get("sy") or "").strip()),
                "_line_ja": line_ja,
                "_suki_line": line_ja,
                "_station": pname or None,
                "_dest": dest,
                "_direction": direction,
                "_number_prefix": number or None,
            })
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "i.opentidkeio.jp",
    }
