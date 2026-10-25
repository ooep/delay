"""Bake data/lines.json: the small bridge dokorail railway ids -> line
titles + ordered station names (+ realtime operator list). Refreshed
weekly; delays runs never touch the geometry catalog."""

import argparse
import json
from pathlib import Path

from . import common


def bake(source, data_dir):
    cat = source.geometry_catalog()
    lines = {}
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
            lines[rail["id"]] = {
                "ja": rail["title"]["ja"],
                "en": rail["title"]["en"],
                "realtime": bool(rail.get("realtime", op.get("realtime"))),
                "stations": stations,
                "stations_by_id": by_id,
            }
    rt_ops = sorted(
        {
            op["operator"]
            for op in cat["operators"]
            for rail in op["railways"]
            if rail.get("realtime", op.get("realtime"))
        }
    )
    return {"realtime_operators": rt_ops, "lines": lines}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--offline", default=None)
    args = ap.parse_args()
    payload = bake(common.Source(args.offline), args.data_dir)
    out = Path(args.data_dir) / "lines.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    print(f"lines: {len(payload['lines'])} railways, realtime ops: {', '.join(payload['realtime_operators'])}")


if __name__ == "__main__":
    main()
