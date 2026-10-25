"""Bake data/delays.json: the current delayed-train list from dokorail,
each entry mapped onto jr.suki.ing timetable keys.

One feed snapshot per run, per-operator fault tolerance (JR-East answers
403 today; it joins automatically once dokorail enables it), and only the
bundles of lines that actually have delayed trains get downloaded. The
output is deliberately small (~100 trains) and site-ready: `by_suki_key`
is a flat lookup table so jr.suki.ing tags its trains with zero join logic.
"""

import argparse
import json
import re
from pathlib import Path

from . import common, suki

DELAY_THRESHOLD_SECONDS = 60
MAX_CANDIDATES = 3


def hhmm_to_min(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def title_index(lines_catalog):
    """Line base title (parenthetical section suffixes stripped, e.g.
    東北本線（黒磯－仙台） -> 東北本線; the JR prefix is dropped too since
    dokorail titles mix both, like JR越後線) -> dokorail railway ids."""
    jr = re.compile(r"^JR")
    idx = {}
    for rid, v in lines_catalog.items():
        base = jr.sub("", common.PAREN_RE.sub("", v["ja"]).strip()).strip()
        idx.setdefault(base, []).append(rid)
    return idx


def norm_station_key(name):
    """Station names normalized for cross-dataset comparison: parenthetical
    disambiguators dropped (粉浜 stays 粉浜, 追分(北海道) -> 追分) and the
    small-ke connector unified (霞ヶ関/霞ケ関)."""
    return common.PAREN_RE.sub("", name or "").strip().replace("ヶ", "ケ").replace("ｹ", "ケ")


class LegStore:
    """Decode + cache timetable legs for the railways that have delayed
    trains this run (a handful of bundle downloads)."""

    def __init__(self, source, index, lines, today_iso, weekday, now_min):
        self.source = source
        self.index = index
        self.lines = lines
        self.today = today_iso
        self.weekday = weekday
        self.now_min = now_min
        self.by_railway = {}
        self.by_number = {}

    def railway_legs(self, railway):
        if railway in self.by_railway:
            return self.by_railway[railway]
        entry = self.index.get(railway)
        line = self.lines.get(railway)
        legs = []
        if entry and line and entry.get("bundle"):
            names = line["stations"]
            for cal in self.calendar_candidates(entry, self.weekday, self.today):
                asset = entry["bundle"]["calendars"].get(cal)
                if not asset:
                    continue
                for block in common.bundle_blocks(self.source.bundle(asset["url"]) or []):
                    if block[2] != railway:
                        continue
                    for number, stops, meta in block[5]:
                        dates = meta.get("dates")
                        if dates and self.today not in common.dated_runs(dates[1], dates[0]):
                            continue
                        decoded = common.decode_stops(stops, names)
                        leg = {
                            "railway": railway,
                            "number": number,
                            "type": meta.get("type"),
                            "names": [n["ja"] for n in meta.get("names") or []],
                            "stops": decoded,
                            "dep": decoded[0]["dep"],
                            "arr": decoded[-1].get("arr") or decoded[-1]["dep"],
                            "calendar": cal,
                        }
                        legs.append(leg)
                if legs:
                    break  # first calendar that actually has this railway's trains
        self.by_railway[railway] = legs
        return legs

    @staticmethod
    def calendar_candidates(entry, weekday, today_iso):
        """Calendar-name candidates for the date, in preference order.
        When the railway has a Dated bundle covering today, it comes first -
        that's what dokorail's own client uses (per-date schedules override
        the generic Weekday/SaturdayHoliday tables). Naming dialects vary
        (JR lines: Weekday/SaturdayHoliday/Dated; Nankai:
        Weekday/Saturday/Holiday), so anything else available trails as a
        last resort."""
        bundle = entry.get("bundle") or {}
        avail = list(bundle.get("calendars") or {})
        order = []
        dated = entry.get("dated") or {}
        if dated.get("start"):
            try:
                from datetime import date, timedelta

                start = date.fromisoformat(dated["start"])
                end = start + timedelta(days=int(dated.get("days", 0)) - 1)
                if start <= date.fromisoformat(today_iso) <= end:
                    order.append("Dated")
            except Exception:
                pass
        if weekday < 5:
            order.append("Weekday")
        elif weekday == 5:
            order += ["SaturdayHoliday", "Saturday"]
        else:  # Sunday: Holiday dialect before Saturday
            order += ["SaturdayHoliday", "Holiday", "Saturday"]
        order += ["Weekday", "SaturdayHoliday", "Holiday", "Saturday", "Dated"]
        return [c for c in dict.fromkeys(order) if c in avail]

    def find_leg(self, railway, number, op=None, railways=None, number_prefix=None):
        """Best leg for a feed train, operator-scoped. `railways` lists the
        dokorail railways to search (self-contained operators like JR-Kyushu
        know their routes); with `number_prefix`, legs match by number
        prefix (feed gives bare "3222", bundles carry "3222M"). Ties prefer
        the leg whose window brackets now, then the shortest suffix."""
        if railways:
            pools = [self.railway_legs(r) for r in railways]
        elif railway:
            pools = [self.railway_legs(railway)]
            if op:
                pools.append(
                    [
                        leg
                        for legs in self.by_railway.values()
                        for leg in legs
                        if leg["railway"] != railway
                        and leg["railway"].startswith(op + ".")
                    ]
                )
        else:
            pools = []
        base = number_prefix or number or ""

        def match_pool(pool):
            exact = [l for l in pool if l["number"] == base]
            if exact:
                return self._closest(exact)
            cands = [l for l in pool if l["number"].startswith(base)]
            if cands:
                # "3222M" beats a longer coincidental "32225M"
                cands.sort(key=lambda l: (len(l["number"]),))
                return self._closest(cands)
            legs = [l for l in pool if l["number"].startswith(base + ".")]
            if legs:
                return self._closest(legs)
            return None

        for pool in pools:
            hit = match_pool(pool)
            if hit:
                return hit
        return None

    def _closest(self, legs):
        def window(leg):
            dep, arr = hhmm_to_min(leg["dep"]), hhmm_to_min(leg["arr"])
            return 0 if dep <= self.now_min <= arr else min(abs(self.now_min - dep), abs(self.now_min - arr))

        return sorted(legs, key=window)[0]

    def find_leg_by_position(self, railways, station, delay_min, tolerance=20):
        """ID-less matching: the feed reports "at <station>, N min late" -
        return the leg whose timetable has it stopping at that station
        around (now - N). Stops AND times both participate (停站和时间基本
        吻合); ties resolve to the nearest scheduled passage. For number-less
        feeds (Hanshin-style slot boards) and as fallback when a feed number
        has no dokorail leg (e.g. Nankai extras). Returns (leg, gap_min)."""
        if not station:
            return None, None
        target = norm_station_key(station)
        if not target:
            return None, None
        pass_time = self.now_min - delay_min
        best, best_gap = None, None
        for rid in railways or []:
            for leg in self.railway_legs(rid):
                for s in leg["stops"]:
                    if norm_station_key(s["station"]) != target:
                        continue
                    arr = hhmm_to_min(s.get("arr") or s["dep"])
                    dep = hhmm_to_min(s["dep"])
                    gap = abs((arr + dep) / 2 - pass_time)
                    if best_gap is None or gap < best_gap:
                        best, best_gap = leg, gap
                    break  # first occurrence of the station on this leg
        if best is not None and best_gap <= tolerance:
            return best, round(best_gap, 1)
        return None, None


def bake(source, data_dir):
    lines = json.loads((Path(data_dir) / "lines.json").read_text())
    realtime_ops = sorted(lines["realtime_operators"])
    # Toei/Keikyu/Tobu/YokohamaMunicipal now come from ODPT (baker/odpt.py):
    # dokorail's own feeds for these 403 GitHub runner IPs via Cloudflare
    realtime_ops = [
        op for op in realtime_ops
        if op not in {"JR-West", "Toei", "Keikyu", "Tobu", "YokohamaMunicipal"}
    ]
    try:
        index = source.timetable_index()["railways"]
    except Exception as exc:  # noqa: BLE001 - flaky CDN: degrade to no-legs
        print(f"timetable index unavailable ({exc}); baking without legs")
        index = {}
    titles = title_index(lines["lines"])
    now = common.jst_now()
    store = LegStore(source, index, lines["lines"], now.date().isoformat(), now.weekday(), now.hour * 60 + now.minute)
    suki_index = suki.SukiIndex.load(data_dir)

    board = []
    op_stats = {}
    feeds = [(op, source.train_feed(op)) for op in realtime_ops]
    # Operators with their own position services (suspended/403 -> None and
    # silently skipped, auto-join on recovery):
    feeds.append(("JR-Shikoku", source.jr_shikoku_feed()))
    feeds.append(("JR-Hokkaido", source.jr_hokkaido_feed()))
    feeds.append(("JR-Kyushu", source.jr_kyushu_feed()))
    feeds.append(("JR-East", source.jr_east_dokotrain_feed()))
    feeds.append(("Nankai", source.nankai_feed()))
    feeds.append(("JR-Central", source.jr_central_feed()))
    feeds.append(("Keihan", source.keihan_feed()))
    feeds.append(("JR-West", source.jr_west_guide_feed()))
    # ODPT operators (Toei keyless; Keikyu/Tobu/YokohamaMunicipal keyed)
    for _odpt_op in ("Toei", "Keikyu", "Tobu", "YokohamaMunicipal"):
        feeds.append((_odpt_op, source.odpt_feed(_odpt_op)))
    for op, payload in feeds:
        if not payload or not payload.get("ok"):
            op_stats[op] = {"feed": "unavailable"}
            print(f"feed unavailable: {op}")
            continue
        body = payload.get("body") or []
        delayed = 0
        for t in body:
            delay = t.get("odpt:delay")
            if delay is None or delay < DELAY_THRESHOLD_SECONDS:
                continue
            delayed += 1
            railway = (t.get("odpt:railway") or "").replace("odpt.Railway:", "")
            line = lines["lines"].get(railway, {})
            # self-contained operators (JR-Hokkaido) carry their own line
            # name and stop pattern; dokorail-based ones use the catalog
            line_ja = line.get("ja") or t.get("_line_ja")
            suki_line = line.get("ja") or t.get("_suki_line")
            entry = {
                "op": op,
                "line": line_ja or railway,
                "railway": railway,
                "number": t.get("odpt:trainNumber"),
                "type": (t.get("odpt:trainType") or "").rsplit(".", 1)[-1] or None,
                "delay_min": round(delay / 60),
                "delay_s": delay,
                "from": line.get("stations_by_id", {}).get(
                    (t.get("odpt:fromStation") or "").rsplit(".", 1)[-1]
                ),
                "cars": t.get("odpt:carComposition"),
            }
            if t.get("_station"):
                entry["from"] = t["_station"]
            if t.get("_or_more"):
                # JR-Kyushu "+N分以上": the value is a floor, not exact
                entry["delay_or_more"] = True
            stations = None
            sched = t.get("_sched")
            if sched and not sched.get("_stale"):
                stations = sched["stops"]
                entry["scheduled"] = {k: v for k, v in sched.items() if not k.startswith("_")}
            else:
                railways_hint = t.get("_railways")
                if not railways_hint and t.get("_line_ja"):
                    # line-name-only sources (doko-train, JR-Central): resolve
                    # the title to dokorail railways, scoped to the operator -
                    # 東海道本線 exists at three JRs, numbers can collide
                    railways_hint = [
                        r for r in titles.get(t["_line_ja"], [])
                        if r.startswith(op + ".")
                    ]
                for rid in railways_hint or []:
                    store.railway_legs(rid)
                leg = store.find_leg(
                    railway,
                    t.get("odpt:trainNumber"),
                    op=op,
                    railways=railways_hint,
                    number_prefix=t.get("_number_prefix"),
                )
                match_method = "number" if leg else None
                pos_gap = None
                if not leg and t.get("_station"):
                    # position-time fallback: the train reports where it is
                    # and how late - find the timetable leg that fits
                    leg, pos_gap = store.find_leg_by_position(
                        railways_hint, t["_station"], round(delay / 60)
                    )
                    match_method = "position" if leg else None
                if leg:
                    stations = [s["station"] for s in leg["stops"]]
                    entry["railway"] = leg["railway"]
                    entry["match"] = match_method
                    if match_method == "position":
                        entry["position_gap_min"] = pos_gap
                    entry["scheduled"] = {
                        "dep": leg["dep"],
                        "arr": leg["arr"],
                        "origin": stations[0],
                        "destination": stations[-1],
                        "stops": stations,
                    }
            if stations:
                cands = suki_index.match(stations, suki_line, railway=railway)
                if cands:
                    entry["suki"] = cands
            board.append(entry)
        op_stats[op] = {"fetched_at": payload.get("fetchedAt"), "trains": len(body), "delayed": delayed}

    board.sort(key=lambda e: (-e["delay_s"], e["railway"], e["number"] or ""))
    by_key = {}
    for e in board:
        for c in e.get("suki", []):
            # strong matches only for the flat lookup table: exact ordered
            # containment, or off by at most one stop (stopping-pattern drift
            # between the datasets); looser candidates stay on the train
            if not (c["subseq"] or c.get("fuzzy", 99) <= 1):
                continue
            slot = by_key.setdefault(
                c["key"],
                {
                    "key": c["key"],
                    "delay_min": e["delay_min"],
                    "train": e["number"],
                    "line": e["line"],
                    "type": e["type"],
                    "dep": e.get("scheduled", {}).get("dep"),
                    "score": c["overlap"],
                    "fuzzy": 0 if c["subseq"] else c.get("fuzzy"),
                    "ambiguous": False,
                    "alternatives": [],
                },
            )
            if slot["train"] != e["number"]:
                slot["ambiguous"] = True
                slot["alternatives"].append({"train": e["number"], "line": e["line"], "delay_min": e["delay_min"]})
    return {
        "version": 1,
        "generated_at": now.isoformat(),
        "date": now.date().isoformat(),
        "delayed_count": len(board),
        "matched_suki_count": len(by_key),
        "operators": op_stats,
        "trains": board,
        "by_suki_key": by_key,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--offline", default=None)
    args = ap.parse_args()
    payload = bake(common.Source(args.offline), args.data_dir)
    out = Path(args.data_dir) / "delays.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    # sukijr.json: the strictly-one-to-one simplification the site asked for
    # - {tmaps_key: delay_minutes}, unambiguous strong matches only.
    # Ambiguous keys (same stopping pattern, several delayed services) stay
    # in delays.json's by_suki_key with alternatives for time tie-breaking.
    sukijr = {
        k: v["delay_min"]
        for k, v in payload["by_suki_key"].items()
        if not v["ambiguous"]
    }
    (Path(args.data_dir) / "sukijr.json").write_text(
        json.dumps(sukijr, ensure_ascii=False, separators=(",", ":"))
    )
    with_legs = sum(1 for t in payload["trains"] if t.get("scheduled"))
    with_suki = sum(1 for t in payload["trains"] if t.get("suki"))
    print(
        f"delays: {payload['delayed_count']} delayed, "
        f"{with_legs} with dokorail leg, {with_suki} mapped to suki keys "
        f"({payload['matched_suki_count']} unique keys, "
        f"{len(sukijr)} one-to-one) -> {out}"
    )


if __name__ == "__main__":
    main()
