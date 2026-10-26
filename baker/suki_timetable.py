"""jr.suki.ing's own timetable (trains_wd/sa/su.json) as the time-aware
disambiguator for suki-key mapping.

Decode verified against the site's own parseTrip() in index.html:
    T[i].t = tmaps key (the pattern this run belongs to)
    T[i].s = "depMin,run1,dwell1,run2,dwell2,..."
        dep[0] = s[0]  minutes from midnight at the origin
        arr[i] = dep[i-1] + s[2i-1]   (run minutes)
        dep[i] = arr[i] + s[2i]       (dwell; last station has none)

Same pattern all day = same key with several T runs. A delayed train's
scheduled departure therefore identifies WHICH key actually has a service
at that minute - this turns the station-sequence candidates (often several
recurring patterns) into a unique, time-verified mapping without any
client-side tie-breaking.
"""

import bisect
import json
from pathlib import Path


def _calendar_file(weekday):
    if weekday == 5:
        return "trains_sa.json"
    if weekday == 6:
        return "trains_su.json"
    return "trains_wd.json"


class SukiTimetable:
    """key -> sorted dep-minute list for the day's calendar, plus the full
    run/dwell series per key (for passage-time lookups by station)."""

    def __init__(self, runs_by_key, series_by_key, source_file):
        self.runs = runs_by_key
        self._series = series_by_key
        self.source_file = source_file

    @classmethod
    def load(cls, data_dir, weekday):
        p = Path(data_dir) / "suki" / _calendar_file(weekday)
        if not p.exists():
            return None
        try:
            d = json.loads(p.read_text())
        except Exception:
            return None
        runs = {}
        series = {}
        for t in d.get("T") or []:
            if not isinstance(t, dict):
                continue
            key = str(t.get("t"))
            parts = str(t.get("s") or "").split(",")
            try:
                nums = [int(x) for x in parts if x != ""]
                dep0 = nums[0]
            except (ValueError, IndexError):
                continue
            runs.setdefault(key, []).append(dep0)
            # "depMin,run1,dwell1,run2,dwell2,..." - run minutes at odd slots,
            # dwell minutes at even slots (after every stop but the last)
            series.setdefault(key, []).append(
                {
                    "dep0": dep0,
                    "runs": nums[1::2],
                    "dwells": nums[2::2],
                }
            )
        for lst in runs.values():
            lst.sort()
        return cls(runs, series, p.name)

    def passage_times(self, key, stops, station):
        """Arrival minute at `station` for every run of `key`.
        `stops` must be the key's normalized station list (same norm as
        `station`). Returns sorted [(arrive_min, dep0), ...].

        Arrivals accumulate run+dwell alternately: arr[i]=dep[i-1]+run[i],
        dep[i]=arr[i]+dwell[i] (no dwell after the final station), so the
        arrival at station idx is dep0 + Σ(run[i]+dwell[i]) for i<idx.
        Runs that don't even reach the station (shorter patterns) are
        skipped by construction of the series."""
        recs = self._series.get(str(key))
        if not recs or station not in stops:
            return []
        idx = stops.index(station)
        out = []
        for r in recs:
            dep0 = r["dep0"]
            runs = r["runs"]
            dwells = r.get("dwells") or []
            if idx > len(runs):
                continue
            acc = dep0
            for i in range(idx):
                acc += runs[i]
                if i < len(dwells):
                    acc += dwells[i]
            out.append((acc, dep0))
        out.sort()
        return out

    def run_near(self, key, dep_min, tol=4):
        """Dep minute of a run of `key` within ±tol of dep_min, or None."""
        lst = self.runs.get(str(key))
        if not lst:
            return None
        i = bisect.bisect_left(lst, dep_min - tol)
        if i < len(lst) and lst[i] <= dep_min + tol:
            return lst[i]
        return None

    def verify_candidates(self, cands, dep_hhmm, tol=4):
        """Split candidates into time-verified and the rest.
        dep_hhmm "HH:MM" (hours may exceed 23 for past-midnight services)."""
        if not dep_hhmm:
            return [], list(cands)
        try:
            h, m = dep_hhmm.split(":")
            dep_min = int(h) * 60 + int(m)
        except ValueError:
            return [], list(cands)
        verified, rest = [], []
        for c in cands:
            run = self.run_near(c["key"], dep_min, tol)
            if run is not None:
                c2 = dict(c)
                c2["dep_run"] = f"{run // 60:02d}:{run % 60:02d}"
                verified.append(c2)
            else:
                rest.append(c)
        return verified, rest
