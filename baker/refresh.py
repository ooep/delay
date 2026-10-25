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
    slim = None
    try:
        cat = source.geometry_catalog()
        slim = {"realtime_operators": set(), "lines": {}}
        for op in cat["operators"]:
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
    except Exception as exc:
        # committed caches keep the bake functional; report and continue
        print(f"lines.json refresh failed ({type(exc).__name__}: {str(exc)[:100]}); keeping committed copy")

    suki = root / "suki"
    suki.mkdir(parents=True, exist_ok=True)
    # SUKI_STATIC already ends in /data - never double it
    files = [
        ("tmaps.json", "tmaps.json"),
        ("lines_map.json", "lines_map.json"),
        ("trains_wd.json", "trains_wd.json"),
        ("trains_sa.json", "trains_sa.json"),
        ("trains_su.json", "trains_su.json"),
    ]
    for local_name, remote_name in files:
        try:
            (suki / local_name).write_bytes(common.http_get(f"{common.SUKI_STATIC}/{remote_name}"))
        except Exception as exc:
            print(f"suki/{local_name} refresh failed ({type(exc).__name__}: {str(exc)[:100]}); keeping committed copy")
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
    if slim:
        print(f"refreshed: {len(slim['lines'])} lines, realtime ops: {', '.join(slim['realtime_operators'])}")
    else:
        print("refresh failed; committed caches remain in place")


if __name__ == "__main__":
    main()
