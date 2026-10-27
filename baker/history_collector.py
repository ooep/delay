"""Delay history collector - completely independent from the bake pipeline.

Replays the finished data/delays.json snapshots that the bake workflow
committed to git, and accumulates per-train delay history into monthly
aggregate files:

    data/delay-history/2026-10.json.gz  one month of per-train records
    data/delay-history/index.json       month -> {total, size_kb} index

Storage is gzip-compressed JSON (~9x smaller): a full month of every
delayed train in Japan fits in ~1.5 MB, ~15 MB for a whole year. Clients
decompress with the standard DecompressionStream('gzip') API.

Design goals:
- Zero coupling: does not touch baker/delays.py, bake-delays.yml or
  data/delays.json; runs as its own workflow on its own schedule.
- Zero interference with the live bake: the history workflow only runs
  once per day during the overnight maintenance window (JST 02:30), so it
  never competes with bake's push. It reads yesterday's snapshots from the
  git history (each bake commits data/delays.json on change) and rebuilds
  that service day's records idempotently - re-running the same day
  produces byte-identical output.
- Minimal repo footprint: one file per MONTH (13 files/year incl. index),
  compact field names, timestamps stored as HH:MM (date lives in the month
  file name + per-record M-D).
- Pagination-friendly: meta.total gives the count without scanning arrays;
  trains[] stays insertion-ordered so clients can offset/limit slice.

Record key: (date, line, number) - one aggregate per train service per day.
Within a month the same train seen on multiple days gets one record per
day (date field differs), so the daily history is preserved.

Run:  python -m baker.history_collector --data-dir data
"""

import argparse
import gzip
import json
import os
import subprocess
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


def _load(path):
    default_month = path.name.removesuffix(".gz").removesuffix(".json")
    try:
        if str(path).endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return json.load(f)
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"meta": {"year_month": default_month, "total": 0}, "trains": []}


def _save(path, payload):
    payload["meta"]["total"] = len(payload["trains"])
    # Content-identity check: only bump last_updated (and rewrite the file)
    # when the train set actually changed. Runs once a day, so this is just
    # a safety net against rewriting an identical file.
    old = None
    try:
        if str(path).endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8") as f:
                old = json.load(f)
        else:
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
    if str(path).endswith(".gz"):
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    else:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    return True


def _git(args, cwd):
    r = subprocess.run(["git"] + list(args), capture_output=True, text=True, cwd=str(cwd))
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr[:400]}")
    return r.stdout.strip()


def _snapshots_for_day(jst_day, repo_root):
    """Collect every delays.json snapshot committed during JST service day
    `jst_day` (05:00 -> next day 02:00), ordered by generated_at.
    Returns [(datetime|None, trains_list), ...]."""
    d = datetime.strptime(jst_day, "%Y-%m-%d").replace(tzinfo=JST)
    # JST day D window [D 05:00, D+1 02:00) == UTC [D-1 20:00, D 17:00)
    utc_from = (d - timedelta(days=1)).replace(hour=20, minute=0, tzinfo=timezone.utc)
    utc_until = d.replace(hour=17, minute=0, tzinfo=timezone.utc)
    shas = _git(
        [
            "log",
            "--since", utc_from.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "--until", utc_until.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "--format=%H",
            "--", "data/delays.json",
        ],
        cwd=repo_root,
    )
    snaps = []
    for sha in shas.splitlines():
        raw = _git(["show", f"{sha}:data/delays.json"], cwd=repo_root)
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        trains = data.get("trains") or []
        if not trains:
            continue
        try:
            ts = datetime.fromisoformat(data["generated_at"])
        except (KeyError, TypeError, ValueError):
            ts = None
        snaps.append((ts, trains))
    # Stable order: by generated_at, then by commit order as found by git log
    snaps.sort(key=lambda x: x[0] or datetime.min.replace(tzinfo=JST))
    return snaps


def replay_day(data_dir, jst_day, repo_root=None):
    """Rebuild one JST service day's records by replaying the day's
    delays.json snapshots from git history. Idempotent: the target day's
    records are removed from the month file and replaced with the rebuilt
    set, so re-running the same day yields the same output.

    Returns (month_path, index_path, trains_in_day)."""
    data_dir = Path(data_dir)
    repo_root = Path(repo_root or data_dir)
    snaps = _snapshots_for_day(jst_day, repo_root)
    if not snaps:
        return None, None, 0

    # JST date -> month file (one service day always maps to one month)
    d = datetime.strptime(jst_day, "%Y-%m-%d").replace(tzinfo=JST)
    month = d.strftime("%Y-%m")
    day = d.strftime("%m-%d")
    hist_dir = data_dir / HISTORY_DIR
    month_path = hist_dir / f"{month}.json.gz"
    index_path = hist_dir / INDEX_NAME
    payload = _load(month_path)
    if "trains" not in payload:
        payload["trains"] = []

    # Keep other days untouched
    kept = [x for x in payload["trains"] if x.get("date") != day]
    # Rebuild this day: key = (no, line)
    day_map = {}
    for ts, trains in snaps:
        hm = ts.strftime("%H:%M") if ts else ""
        for t in trains:
            rec = _record(t, day)
            if not rec["no"] or not rec["l"]:
                continue
            k = (rec["no"], rec["l"])
            if k in day_map:
                cur = day_map[k]
                if rec["md"] > cur["md"]:
                    cur["md"] = rec["md"]
                cur["s"] += 1
                if hm and (not cur["ls"] or hm > cur["ls"]):
                    cur["ls"] = hm
            else:
                rec["fs"] = hm
                rec["ls"] = hm
                day_map[k] = rec
    day_recs = sorted(day_map.values(), key=lambda r: (r["no"], r["l"]))
    payload["trains"] = kept + day_recs

    changed = _save(month_path, payload)
    if _index_dirty(index_path, month, payload, month_path.stat().st_size):
        _update_index(index_path, month, payload, month_path.stat().st_size)
    return month_path, index_path, len(day_recs)


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
    ap.add_argument(
        "--repo-root", default=None,
        help="git repo root (defaults to the current working directory, "
        "which is the checkout root in Actions)",
    )
    ap.add_argument(
        "--replay-date", default=None,
        help="JST service day to rebuild, YYYY-MM-DD (default: yesterday JST)",
    )
    args = ap.parse_args()
    if args.replay_date is None:
        args.replay_date = (jst_now() - timedelta(days=1)).strftime("%Y-%m-%d")
    # Git commands must run from the repo root; in Actions that is the
    # checkout working directory (data lives in <root>/data).
    repo_root = Path(args.repo_root) if args.repo_root else Path(os.getcwd())
    repo_root = repo_root.resolve()
    data_dir = Path(args.data_dir).resolve()
    try:
        month_path, index_path, n = replay_day(data_dir, args.replay_date, repo_root)
    except RuntimeError as e:
        print(f"history: replay failed: {e}", file=sys.stderr)
        return 1
    if month_path is None:
        print(f"history: no delays.json snapshots found for {args.replay_date}; nothing to do")
        return 0
    total = _load(month_path)["meta"]["total"]
    print(
        f"history: {args.replay_date} -> {month_path.name}: {n} delayed trains recorded"
        f" (total {total})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())


if __name__ == "__main__":
    sys.exit(main())
