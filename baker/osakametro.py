"""Osaka Metro (大阪市高速電気軌道) live position/congestion feed.

NOT a delay source: the official trainlocation API carries no delay field
(position inference only), so this module produces a standalone payload
data/osakametro-congestion.json (keikyu-positions.json precedent) instead of
entering the delays.json main flow.

Source: the official web SPA (subway.osakametro.co.jp/ressyaichi.php) talks to
    GET https://api.mobility-operation-info.emetro-app.osakametro.co.jp/app/api/v1/trainlocation?route_code=N
    X-Api-Key: <public key shipped in the SPA main.js>
Line master (route_code -> line name):
    https://static.mobility-operation-info.emetro-app.osakametro.co.jp/emetro/cache/common/json/route.json

The X-Api-Key is the public key shipped in the official web SPA's main.js
(reverse-engineered 2026-10-26; also published in Train Monitor gists). It is
NOT a secret, but if Osaka Metro rotates it this source silently empties -
fetch() treats an empty/401 response as a source failure and returns None, so
the main bake never breaks (single-source fault tolerance).

Run with:  python -m baker.osakametro [--data-dir data]
"""

import argparse
import json
import time

from . import common

API = "https://api.mobility-operation-info.emetro-app.osakametro.co.jp/app/api/v1"
TRAINLOC_URL = API + "/trainlocation"
ROUTE_URL = (
    "https://static.mobility-operation-info.emetro-app.osakametro.co.jp/"
    "emetro/cache/common/json/route.json"
)
API_KEY = "XSGUG4p5Ya5vQCehV3zZjaDheZAQMpqP9paVan8W"

CONGESTION_PATH = "osakametro-congestion.json"


def _headers():
    return {"X-Api-Key": API_KEY}


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
