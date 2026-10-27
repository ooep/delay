"""Delay history collector - completely independent from the bake pipeline.

Reads the finished data/delays.json (read-only; never modifies it) and
accumulates per-train delay history into monthly aggregate files:

    data/delay-history/2026-10.json     one month of per-train records
    data/delay-history/index.json       month -> {total, size_kb} index

Design goals:
- Zero coupling: does not touch baker/delays.py, bake-delays.yml or
  data/delays.json; runs as its own workflow on its own schedule.
- Minimal repo footprint: one file per MONTH (13 files/year incl. index),
  compact field names, timestamps stored as HH:MM (date lives in the month
  file name + per-record M-D).
- Pagination-friendly: meta.total gives the count without scanning arrays;
  trains[] stays insertion-ordered so clients can offset/limit slice.

Record key: (op, line, number) - one aggregate per train service per day.
Within a month the same train seen on multiple days gets one record per
day (date field differs), so the daily history is preserved.

Run:  python -m baker.history_collector --data-dir data
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

HISTORY_DIR = "delay-history"
INDEX_NAME = "index.json"


def jst_now():
    return datetime.now(JST)


def _record(t, day):
    """Compact record from one delayed-train entry in delays.json."""
    return {
        "date": day,          # M-D, year lives in the month file name
        "no": t.get("number") or "",        # train number
        "l": t.get("line") or "",           # line name
        "t": t.get("type") or "",           # train type (普通/快速/特急...)
        "md": int(t.get("delay_min") or 0), # max delay minutes observed
        "fs": "",             # first seen HH:MM (filled at write time)
        "ls": "",             # last seen HH:MM
        "s": 1,               # snapshot count
    }


def _upsert(trains, rec, now_hm):
    """Merge rec into trains[] keyed by (date, no, l). Updates max delay and
    last-seen when the same train reappears; returns updated list."""
    key = (rec["date"], rec["no"], rec["l"])
    for i, x in enumerate(trains):
        if (x["date"], x["no"], x["l"]) == key:
            if rec["md"] > x["md"]:
                x["md"] = rec["md"]
            x["ls"] = now_hm
            x["s"] += 1
            return trains
    rec["fs"] = now_hm
    rec["ls"] = now_hm
    trains.append(rec)
    return trains


def _load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"meta": {"year_month": path.stem, "total": 0}, "trains": []}


def _save(path, payload):
    payload["meta"]["total"] = len(payload["trains"])
    # Content-identity check: only bump last_updated (and rewrite the file)
    # when the train set actually changed. Without this, a 5-min cron would
    # produce a commit every tick just because the timestamp moved.
    old = None
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if (
        old is not None
        and old.get("meta", {}).get("year_month") == payload["meta"].get("year_month")
        and old.get("meta", {}).get("total") == payload["meta"]["total"]
        and old.get("trains") == payload["trains"]
    ):
        return False
    payload["meta"]["last_updated"] = jst_now().strftime("%Y-%m-%dT%H:%M:%S+09:00")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    return True


def collect(data_dir, now=None):
    """Read data/delays.json and merge into the current month's history file.
    Returns (month_file, index_file, added, updated) counts."""
    now = now or jst_now()
    data_dir = Path(data_dir)
    delays_path = data_dir / "delays.json"
    if not delays_path.exists():
        return None, None, 0, 0

    trains_in = (json.loads(delays_path.read_text(encoding="utf-8")) or {}).get("trains") or []
    if not trains_in:
        return None, None, 0, 0

    month = now.strftime("%Y-%m")
    day = now.strftime("%m-%d")
    now_hm = now.strftime("%H:%M")

    hist_dir = data_dir / HISTORY_DIR
    month_path = hist_dir / f"{month}.json"
    index_path = hist_dir / INDEX_NAME

    payload = _load(month_path)
    if "trains" not in payload:
        payload["trains"] = []
    trains = payload["trains"]

    added = updated = 0
    for t in trains_in:
        rec = _record(t, day)
        if not rec["no"] or not rec["l"]:
            continue
        key = (rec["date"], rec["no"], rec["l"])
        found = False
        for x in trains:
            if (x["date"], x["no"], x["l"]) == key:
                if rec["md"] > x["md"]:
                    x["md"] = rec["md"]
                x["ls"] = now_hm
                x["s"] += 1
                updated += 1
                found = True
                break
        if not found:
            rec["fs"] = now_hm
            rec["ls"] = now_hm
            trains.append(rec)
            added += 1

    _save(month_path, payload)
    if _index_dirty(index_path, month, payload, month_path.stat().st_size):
        _update_index(index_path, month, payload, month_path.stat().st_size)
    return month_path, index_path, added, updated


def _index_dirty(index_path, month, payload, size_bytes):
    try:
        idx = json.loads(index_path.read_text(encoding="utf-8"))
        cur = (idx.get("months") or {}).get(month) or {}
        return cur.get("total") != payload["meta"]["total"] or cur.get("size_kb") != round(size_bytes / 1024, 1)
    except (OSError, ValueError):
        return True


def _update_index(index_path, month, payload, size_bytes):
    idx = {"months": {}}
    try:
        idx = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    size_kb = round(size_bytes / 1024, 1)
    idx["months"][month] = {"total": payload["meta"]["total"], "size_kb": size_kb}
    idx["updated_at"] = jst_now().strftime("%Y-%m-%dT%H:%M:%S+09:00")
    tmp = index_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(idx, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(index_path)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default="data", help="directory holding delays.json")
    args = ap.parse_args()
    month_path, index_path, added, updated = collect(args.data_dir)
    if month_path is None:
        print("history: delays.json missing or empty; nothing to do")
        return 0
    print(
        f"history: {month_path.name} +{added} new, {updated} updated "
        f"(total {json.loads(month_path.read_text())['meta']['total']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
