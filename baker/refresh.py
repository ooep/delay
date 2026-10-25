"""Refresh the slow-moving caches used by the delay bake:
- data/lines.json         dokorail geometry catalog (line bridge)
- data/suki/tmaps.json    jr.suki.ing train-key -> route map
- data/suki/lines_map.json jr.suki.ing line aliases

Runs weekly (Sunday) or whenever files are missing; the per-run delay bake
never touches these endpoints.
"""

import argparse
import json
from pathlib import Path

from . import common

WEEKDAY_SUNDAY = 6  # date.weekday(): Mon=0 ... Sun=6


def needed(data_dir, force):
    root = Path(data_dir)
    if force:
        return True
    for name in ("lines.json", "suki/tmaps.json", "suki/lines_map.json",
                 "suki/trains_wd.json", "suki/trains_sa.json", "suki/trains_su.json"):
        if not (root / name).exists():
            return True
    return common.jst_now().weekday() == WEEKDAY_SUNDAY


def refresh(source, data_dir):
    root = Path(data_dir)
    lines = source.geometry_catalog()
    # keep only what delays.py consumes: station order/names + realtime flag
    slim = {"realtime_operators": set(), "lines": {}}
    for op in lines["operators"]:
        for rail in op["railways"]:
            stations = []
            by_id = {}
            for raw in rail.get("stations") or []:
                names = raw[1] if isinstance(raw[1], dict) else {"ja": str(raw[1]), "en": str(raw[1])}
                ids = raw[0] if isinstance(raw[0], list) else [raw[0]]
                stations.append(names["ja"])
                for sid in ids:
                    by_id[str(sid).rsplit(".", 1)[-1]] = names["ja"]
            rt = bool(rail.get("realtime", op.get("realtime")))
            slim["lines"][rail["id"]] = {
                "ja": rail["title"]["ja"],
                "en": rail["title"]["en"],
                "realtime": rt,
                "stations": stations,
                "stations_by_id": by_id,
            }
            if rt:
                slim["realtime_operators"].add(op["operator"])
    slim["realtime_operators"] = sorted(slim["realtime_operators"])
    (root / "lines.json").write_text(json.dumps(slim, ensure_ascii=False, separators=(",", ":")))

    suki = root / "suki"
    suki.mkdir(parents=True, exist_ok=True)
    (suki / "tmaps.json").write_bytes(common.http_get(common.SUKI_STATIC + "/tmaps.json"))
    (suki / "lines_map.json").write_bytes(common.http_get(common.SUKI_STATIC + "/lines_map.json"))
    # full timetables with times (trains_wd/su/sa.json, ~12 MB each) - staged
    # for time-based one-to-one resolution against delayed trains
    for name in ("trains_wd", "trains_sa", "trains_su"):
        (suki / f"{name}.json").write_bytes(
            common.http_get(common.SUKI_STATIC + f"/data/{name}.json")
        )
    return slim


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if not needed(args.data_dir, args.force):
        print("caches fresh enough, skipping")
        return
    slim = refresh(common.Source(), args.data_dir)
    print(f"refreshed: {len(slim['lines'])} lines, realtime ops: {', '.join(slim['realtime_operators'])}")


if __name__ == "__main__":
    main()
