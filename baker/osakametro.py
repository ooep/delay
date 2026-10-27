"""Osaka Metro (大阪市高速電気軌道) live feeds.

Two channels from the official web SPA (subway.osakametro.co.jp/ressyaichi.php):

1. Train positions + per-car congestion/temperature (no delay field exists):
       GET .../app/api/v1/trainlocation?route_code=N
   produced as a standalone payload data/osakametro-congestion.json
   (keikyu-positions.json precedent) - NOT part of the delays.json flow.

2. Line-level operation status (the only delay-like signal Osaka Metro
   publishes publicly): the SPA's 運行状況 banner pulls
       GET .../app/api/v1/operationinfo
   -> operationInfoList[] (filtered per line by `line` = route_code) with
   {status, happendingTime, cause, from, to, message, transferInfoList[]}.
   fetch_operation() turns non-normal alerts into line-level delay entries
   (5-minute floor, exact official text attached) for the delays main flow -
   the same convention as baker/tokyometro_unkou.py. All lines normal ->
   empty body (the board shows nothing for this operator, which is correct).

Line master (route_code -> line name / abbreviation):
    https://static.mobility-operation-info.emetro-app.osakametro.co.jp/emetro/cache/common/json/route.json

Auth: X-Api-Key header with the public key shipped in the SPA's main.js
(reverse-engineered 2026-10-26; also published in Train Monitor gists). It is
NOT a secret, but if Osaka Metro rotates it these sources silently empty -
every fetch treats an empty/error response as a source failure and returns
None, so the main bake never breaks (single-source fault tolerance).

Run with:  python -m baker.osakametro [--data-dir data]
"""

import argparse
import json
import time

from . import common

API = "https://api.mobility-operation-info.emetro-app.osakametro.co.jp/app/api/v1"
TRAINLOC_URL = API + "/trainlocation"
OPERATION_URL = API + "/operationinfo"
ROUTE_URL = (
    "https://static.mobility-operation-info.emetro-app.osakametro.co.jp/"
    "emetro/cache/common/json/route.json"
)
API_KEY = "XSGUG4p5Ya5vQCehV3zZjaDheZAQMpqP9paVan8W"

CONGESTION_PATH = "osakametro-congestion.json"

# Line-level announcement floor (minutes): like tokyometro.jp, Osaka Metro
# only announces delays per line without per-train minutes, so a delayed line
# is reported at the 5-minute floor with the exact official status text.
LINE_LEVEL_DELAY_SECONDS = 5 * 60

# route_code -> lines.json railway key (verified against route.json + lines.json
# 2026-10-27; 9 lines, stable):
#   1=御堂筋線(M) 2=谷町線(T) 3=四つ橋線(Y) 4=中央線(C) 5=千日前線(S)
#   6=堺筋線(A) 7=長堀鶴見緑地線(N) 8=今里筋線(I) 9=ニュートラム(P)
ROUTE_TO_RAILWAY = {
    "1": "OsakaMetro.Midosuji",
    "2": "OsakaMetro.Tanimachi",
    "3": "OsakaMetro.Yotsubashi",
    "4": "OsakaMetro.Chuo",
    "5": "OsakaMetro.Sennichimae",
    "6": "OsakaMetro.Sakaisuji",
    "7": "OsakaMetro.NagahoriTsurumiRyokuchi",
    "8": "OsakaMetro.Imazatosuji",
    "9": "OsakaMetro.NewTram",
}

_STATUSES = [
    ("suspended", ("運転を見合わせ", "運転見合わせ", "見合わせ")),
    ("disrupted", ("ダイヤ乱れ", "ダイヤが乱れ", "ダイヤを乱し")),
    ("delayed", ("遅延", "遅れ")),
]


def _headers():
    return {"X-Api-Key": API_KEY}


# ---------------------------------------------------------------------------
# Channel 1: per-train positions / congestion (standalone payload)
# ---------------------------------------------------------------------------

def parse_route(raw):
    """route.json -> {routeCode: routeName} (order preserved by the API)."""
    out = {}
    for r in (raw.get("routeList") or []):
        code = r.get("routeCode")
        if code:
            out[str(code)] = r.get("routeName")
    return out


def parse_trainlocation(raw):
    """trainlocation response -> normalized train rows (position + per-car
    congestion/temperature; the API has no delay field at all)."""
    out = []
    for t in raw.get("trainLocationList") or []:
        if not isinstance(t, dict):
            continue
        out.append(
            {
                "direction": t.get("directionId"),
                "from_station": t.get("fromStationCode"),
                "to_station": t.get("toStationCode"),
                "terminal": t.get("terminalStationCode"),
                "progress": t.get("progress"),
                "vehicle": t.get("vehicleNumber"),
                "congestion": t.get("trainCarCongestionList") or "",
                "temperature": t.get("trainCarTemperatureList") or "",
                "same_as": t.get("sameAs"),
            }
        )
    return out


def build_payload(route_names, trains_by_code, fetched_at, source_time=None):
    routes = {}
    total = 0
    for code in sorted(trains_by_code):
        name = route_names.get(str(code), f"route {code}")
        trs = trains_by_code.get(code) or []
        total += len(trs)
        routes[str(code)] = {"route_name": name, "trains": trs}
    return {
        "version": 1,
        "generated_at": fetched_at,
        "source_time": source_time,
        "train_count": total,
        "routes": routes,
        "note": (
            "Osaka Metro official trainlocation API (public X-Api-Key from the "
            "official web SPA). No delay field exists on this API - position + "
            "per-car congestion/temperature only."
        ),
    }


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        route = json.loads((src.offline.root / "om-route.json").read_text())
        route_names = parse_route(route)
        trains_by_code = {}
        for code in route_names:
            try:
                raw = json.loads(
                    (src.offline.root / f"om-tl-{code}.json").read_text()
                )
                trains_by_code[code] = parse_trainlocation(raw)
            except Exception:
                trains_by_code[code] = []
    else:
        try:
            route = common.get_json(ROUTE_URL, headers=_headers())
        except Exception as exc:
            print(f"osakametro feed: route master: {type(exc).__name__}: {str(exc)[:120]}")
            return None
        route_names = parse_route(route)
        if not route_names:
            print("osakametro feed: empty route master")
            return None
        trains_by_code = {}
        for code in sorted(route_names):
            try:
                raw = common.get_json(
                    TRAINLOC_URL + "?route_code=" + code, headers=_headers()
                )
                trains_by_code[code] = parse_trainlocation(raw)
            except Exception as exc:
                # one route failing must not abort the whole payload
                print(f"osakametro route {code}: {type(exc).__name__}: {str(exc)[:100]}")
                trains_by_code[code] = []
            time.sleep(0.5)
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "routes": route_names,
        "body": trains_by_code,
        "source": "osakametro emetro-app API",
    }


# ---------------------------------------------------------------------------
# Channel 2: line-level operation status -> delays flow
# ---------------------------------------------------------------------------

def _classify(text):
    for status, needles in _STATUSES:
        for n in needles:
            if n in text:
                return status
    return "normal"


def _format_info(item):
    """Reassemble the SPA's alert text: <time> <cause> <from→to>\n<message>
    plus any transfer-range note. Returns a compact official-text string."""
    parts = []
    if item.get("happendingTime"):
        parts.append(str(item["happendingTime"]))
    if item.get("cause"):
        parts.append(str(item["cause"]))
    if item.get("from") or item.get("to"):
        parts.append(f"{item.get('from') or '?'}→{item.get('to') or '?'}")
    head = " ".join(parts)
    text = (head + "\n" if head else "") + str(item.get("message") or "")
    for tr in item.get("transferInfoList") or []:
        if tr.get("transferLineJa") or tr.get("transferFromToJa"):
            text += f"\n【振替輸送実施区間】{tr.get('transferLineJa') or ''} {tr.get('transferFromToJa') or ''}".rstrip()
    return text.strip()[:300]


def parse_operation(raw, route_names):
    """operationinfo response -> line-level delay entries (delays-flow shape).
    Only routes with a non-normal alert are emitted; all-normal -> []."""
    out = []
    by_line = {}
    for item in raw.get("operationInfoList") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("line") or "")
        by_line.setdefault(code, []).append(item)
    for code, items in by_line.items():
        railway = ROUTE_TO_RAILWAY.get(code)
        if not railway:
            continue
        # the SPA takes the first (latest) alert per line
        item = items[0]
        status_text = f"{item.get('status') or ''} {item.get('message') or ''}"
        status = _classify(status_text)
        if status == "normal":
            continue
        out.append(
            {
                "owl:sameAs": f"OsakaMetro:{code}",
                "odpt:railway": f"odpt.Railway:{railway}",
                "odpt:trainNumber": None,
                "odpt:delay": LINE_LEVEL_DELAY_SECONDS,
                "odpt:trainType": None,
                "odpt:fromStation": None,
                "odpt:toStation": None,
                "_line_ja": route_names.get(code) or railway.split(".")[-1],
                "_metro_status": status,
                "_metro_status_text": _format_info(item),
                "_metro_updated": item.get("happendingTime"),
            }
        )
    return out


def fetch_operation(src=None):
    src = src or common.Source()
    if src.offline:
        try:
            raw = json.loads((src.offline.root / "om-operation.json").read_text())
        except Exception:
            return {"ok": True, "body": [], "fetchedAt": common.jst_now().isoformat()}
        try:
            route = json.loads((src.offline.root / "om-route.json").read_text())
        except Exception:
            route = {}
        route_names = parse_route(route)
    else:
        try:
            raw = common.get_json(OPERATION_URL, headers=_headers())
        except Exception as exc:
            print(f"osakametro operation: {type(exc).__name__}: {str(exc)[:120]}")
            return None
        try:
            route = common.get_json(ROUTE_URL, headers=_headers())
        except Exception:
            route = {}
        route_names = parse_route(route)
    body = parse_operation(raw, route_names)
    return {"ok": True, "body": body, "fetchedAt": common.jst_now().isoformat()}


# ---------------------------------------------------------------------------
# CLI (channel 1 standalone payload only; channel 2 runs inside the bake)
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    args = ap.parse_args()
    payload = fetch()
    if not payload or not payload.get("ok"):
        print("osakametro: feed unavailable; payload not updated")
        return 1
    out_payload = build_payload(
        payload["routes"],
        payload["body"],
        payload["fetchedAt"],
        source_time=payload["fetchedAt"],
    )
    from pathlib import Path

    out = Path(args.data_dir) / CONGESTION_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(out_payload, ensure_ascii=False, separators=(",", ":")))
    print(
        f"osakametro congestion: {out_payload['train_count']} trains over "
        f"{len(out_payload['routes'])} routes -> {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
