"""Bake data/delays.json: the current delayed-train list from dokorail,
each entry mapped onto jr.suki.ing timetable keys.

One feed snapshot per run, per-operator fault tolerance (JR-East answers
403 today; it joins automatically once dokorail enables it), and only the
bundles of lines that actually have delayed trains get downloaded. The
output is deliberately small (~100 trains) and site-ready: `by_suki_key`
is a flat lookup table so jr.suki.ing tags its trains with zero join logic.
"""

import argparse
import concurrent.futures
import json
import os
import re
import threading
from pathlib import Path

from . import common, holidays, suki, suki_timetable, jrcentral

DELAY_THRESHOLD_SECONDS = 60
MAX_CANDIDATES = 3
# Parallel feed fetch: sources are independent websites, so we fetch them
# concurrently (the previous serial walk took ~4 min, dominated by network
# waits). Each feed's internal request order stays untouched - JR-Kyushu's
# WAF throttling, doko-train's line scans etc. remain serial inside the feed.
FEED_FETCH_WORKERS = 12
# suki-direct 时刻消歧：报告站过站时刻与 (now - delay) 的容差（分钟）。
# 新干线等无 dokorail 时刻表的线路，用 suki 时刻表把多候选收敛为唯一班次；
# 收敛必须严格（窗口内唯一趟 + 唯一 key + 车种特征吻合），否则宁漏不误报。
DIRECT_TIME_TOLERANCE_MIN = 3


def _type_compatible(stops_norm, train_type):
    """车种特征强校验:候选 key 的停站序列必须符合该车种的实际停站模式,
    防止 さくら 被误标到 のぞみ 模式等混乱。suki 时刻表按 停站序列 分车种:
    鹿児島中央 只有 さくら/みずほ 直通;こだま 站多;のぞみ 不到鹿児島中央。"""
    if not train_type:
        return True
    n = len(stops_norm)
    has_kago = "鹿児島中央" in stops_norm
    if train_type in ("さくら", "みずほ"):
        return has_kago
    if train_type == "のぞみ":
        # 12 站 = 東京⇄博多 全程车(7237/193 实测);11 站 = 博多終点型(8068)
        return not has_kago and n <= 12
    if train_type == "こだま":
        return n >= 12
    if train_type == "ひかり":
        return not has_kago and 10 <= n <= 13
    return True


def _stops_consistent(stops_n, report_station, official):
    """候选 key 的停站序列必须与官方発車順序（departure_info）一致：
    报站之后的候选停站须以官方后续停站为前缀（允许候选继续延伸，如
    さくら 753 官方列表到博多、候选 2456 延伸到鹿児島中央），且官方
    终点站必须落在候选站序中。站名兼容県后缀（小倉(福岡県) ↔ 小倉）。"""
    off_stations = [s for s in (official.get("stations") or []) if s]
    terminal = official.get("terminal")
    try:
        idx = stops_n.index(report_station)
    except ValueError:
        for i, s in enumerate(stops_n):
            if s.startswith(report_station) and s[len(report_station):].endswith(("都", "道", "府", "県")):
                idx = i
                break
        else:
            return False
    tail = stops_n[idx + 1:]
    n_off = len(off_stations)
    # 站数一致性: 候选报站后的站数必须等于官方后续停站数, 或 +1
    # (官方终点站延伸在列表外, 如 753 官方到川内 + 鹿児島中央终点)
    if not (n_off <= len(tail) <= n_off + 1):
        return False
    for i, off in enumerate(off_stations):
        cand = tail[i]
        if cand != off and not (cand.startswith(off) and cand[len(off):].endswith(("都", "道", "府", "県"))):
            return False
    if len(tail) == n_off + 1 and terminal and tail[-1] != terminal:
        # 多出的那一站必须是官方终点站(或带県后缀变体)
        if not (tail[-1].startswith(terminal) and tail[-1][len(terminal):].endswith(("都", "道", "府", "県"))):
            return False
    if terminal:
        ok = terminal in stops_n or any(
            s.startswith(terminal) and s[len(terminal):].endswith(("都", "道", "府", "県"))
            for s in stops_n
        )
        if not ok:
            return False
    return True


def _hhmm_min(hhmm):
    """"HH:MM" (hours may exceed 23) -> minutes from midnight, or None."""
    if not hhmm:
        return None
    try:
        h, m = str(hhmm).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _verify_passage(suki_index, stt, cands, origin_station, dep_hhmm, tol=5):
    """Time-verify suki candidates at the leg's origin station, using each
    candidate's *passage time there* instead of its own dep minute.

    Through / mid-route trains start at a different station than the leg
    (204M 辰野始発 vs 7345 松本始発→辰野通過): dep0-based checks misjudge
    the true candidate by tens of minutes and leave lookalike schedules
    (21:40/6:16 班次) to be tagged. Returns (verified, rest); verified
    candidates carry `dep_run` = the passage minute found. 宁漏不误报."""
    if not dep_hhmm or not origin_station:
        return [], list(cands)
    try:
        h, m = dep_hhmm.split(":")
        dep_min = int(h) * 60 + int(m)
    except ValueError:
        return [], list(cands)
    want = suki.norm_station(origin_station)
    verified, rest = [], []
    for c in cands:
        key = str(c["key"])
        stops_n = suki_index._stops_norm.get(key)
        if not stops_n:
            rest.append(c)
            continue
        # station index: exact match, or the prefecture-suffixed variant
        # (高松(香川県) vs 高松) used by suki's normalized names
        if want in stops_n:
            idx = stops_n.index(want)
        else:
            idx = next(
                (
                    i
                    for i, s in enumerate(stops_n)
                    if s.startswith(want) and s[len(want):].endswith(("都", "道", "府", "県"))
                ),
                None,
            )
        if idx is None:
            rest.append(c)
            continue
        pas = stt.passage_times(key, stops_n, stops_n[idx])
        if not pas:
            rest.append(c)
            continue
        best = min((abs(p - dep_min), p) for p, _ in pas)
        if best[0] <= tol:
            c2 = dict(c)
            c2["dep_run"] = f"{best[1] // 60:02d}:{best[1] % 60:02d}"
            verified.append(c2)
        else:
            rest.append(c)
    return verified, rest


def resolve_direct_by_time(stt, suki_index, cands, station, now, delay_min,
                            train_type=None, tol=DIRECT_TIME_TOLERANCE_MIN,
                            official=None, dest=None):
    """严格强匹配:把 suki-direct 多候选按『报告站过站时刻 ≈ 表定時刻』
    收敛。三条硬约束(参考在来線 find_leg 的匹配语义,宁漏不误报):
    ① 候选 key 的站序须符合车种特征(_type_compatible)，且当官方発車順序
       可用时与官方后续停站序列一致(_stops_consistent);
    ② 该 key 在容差窗口内必须只有一趟(同 key 多趟 → 无法确定具体班次 → 弃);
    ③ 全局唯一:与次优 key 的 gap 差 ≤ 2 → 模式歧义 → 弃。
    official: {departure: 官方表定過站分钟, stations: 后续停站, terminal}。
    表定對表定匹配（delay 是附加信息不参与匹配），缺失时回退 now-delay。
    返回 {"cand": 单候选, "scheduled": {dep, origin, stops}} 或 None。"""
    if official is not None and official.get("departure") is not None:
        est = official["departure"]
    else:
        est = now.hour * 60 + now.minute - (delay_min or 0)
    want = suki.norm_station(station)
    scored = []
    want_dest = suki.norm_station(dest) if dest else None
    for c in cands:
        key = str(c["key"])
        stops_n = suki_index._stops_norm[key]
        # terminus must match the feed's reported destination when known -
        # collapses commuter pools (local to 君津 vs rapid to 館山).
        # Origin is NOT filtered: live feed may only see a through-service
        # mid-route (BCD subset of suki's ABCD) - station order is already
        # guaranteed by match_direct's direction filter.
        if want_dest and stops_n and stops_n[-1] != want_dest:
            continue
        # suki names carry prefecture qualifiers (高松(香川県)) while live
        # feeds report the bare station (高松) - same suffixed-variant rule
        # as match_direct, inside the line-filtered pool
        if want not in stops_n and not suki_index._has_pref_suffixed(stops_n, want):
            continue
        if not _type_compatible(stops_n, train_type):
            continue
        if official is not None and not _stops_consistent(stops_n, want, official):
            continue
        anchor = want if want in stops_n else next(
            s for s in stops_n
            if s.startswith(want) and s[len(want):].endswith(("都", "道", "府", "県"))
        )
        pas = stt.passage_times(key, stops_n, anchor)
        if not pas:
            continue
        near = [p for p in pas if abs(p[0] - est) <= tol]
        if len(near) != 1:
            continue
        arrive, dep0 = near[0]
        scored.append((abs(arrive - est), c, key, arrive, dep0))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0])
    # Exact time match (gap=0) is unambiguous - take it even if a 2nd
    # candidate is close. Otherwise require >2 min gap from runner-up.
    if scored[0][0] > 0 and len(scored) >= 2 and scored[1][0] - scored[0][0] <= 2:
        return None
    gap, cand, key, arrive, dep0 = scored[0]
    if gap > tol:
        return None
    stops = suki_index.trains[key]["s_jas"]
    c2 = dict(cand)
    c2["size"] = len(stops)
    scheduled = {
        "dep": f"{dep0 // 60:02d}:{dep0 % 60:02d}",
        "origin": stops[0],
        "stops": stops,
    }
    return {"cand": c2, "scheduled": scheduled}


def _collapse_by_time(stt, suki_index, cands, station, now, delay_min,
                      tol=10, leg_stops=None, dep_feed=None):
    """Collapse station-sequence-identical suki candidates (loop lines run
    the same number several times a day: 山手線 G trains, 内房線 repeaters)
    to the unique run whose reported-station passage time ≈ now − delay.
    At one moment only one train can be at one station on one path, so a
    unique close passage wins; two nearly-equidistant runs stay ambiguous
    and are dropped (宁漏不误报). The tolerance is generous (10 min) because
    the reported delay itself is only accurate to a few minutes. When every
    candidate is too far off, the temporally closest one wins - the loop
    position estimate has its own error, and station-sequence ties without
    any time evidence resolve to the run whose schedule best brackets now.
    `leg_stops` (the feed leg's stop list) adds a direction guard: loop
    pools contain both directions (11011 外回 vs 11016/10507 内回) and
    passage time alone can pick the wrong one, so a candidate whose next
    station after the reported station differs from the leg's is dropped.
    `dep_feed` (the feed leg's scheduled departure minute) ranks candidates
    by |suki dep0 − feed dep| first: the main site ties by departure minute,
    so two loop keys whose passage gaps tie (10891 vs 11011 both 16 min off
    at 鶯谷) must resolve to the key whose departure actually matches the
    train (2100G dep 21:13 → 11011 dep0 21:00, not 10891 dep0 21:31).
    Returns a single-candidate list, or the original list when no unique
    hit."""
    if not stt or not station or len(cands) < 2:
        return cands
    want = suki.norm_station(station)
    est = now.hour * 60 + now.minute - (delay_min or 0)
    # direction anchor: the leg's next station after the reported one
    leg_next = None
    if leg_stops:
        ln = [suki.norm_station(s) for s in leg_stops]
        try:
            i = ln.index(want)
            leg_next = ln[(i + 1) % len(ln)]
        except ValueError:
            leg_next = None
    scored = []
    for c in cands:
        key = str(c["key"])
        stops_n = suki_index._stops_norm.get(key)
        if not stops_n:
            continue
        if want not in stops_n and not suki_index._has_pref_suffixed(stops_n, want):
            continue
        anchor = want if want in stops_n else next(
            (s for s in stops_n if s.startswith(want) and s[len(want):].endswith(("都", "道", "府", "県"))),
            None,
        )
        if anchor is None:
            continue
        if leg_next is not None:
            # every occurrence of the reported station must agree with the
            # leg direction; if none does, this key runs the wrong way
            ok_dir = False
            for i, s in enumerate(stops_n):
                if s == anchor:
                    nxt = stops_n[(i + 1) % len(stops_n)]
                    if nxt == leg_next or nxt.startswith(leg_next) or leg_next.startswith(nxt):
                        ok_dir = True
                        break
            if not ok_dir:
                continue
        pas = stt.passage_times(key, stops_n, anchor)
        if not pas:
            continue
        # every run of the key participates: a key runs many times a day
        # (11011 外回双圈 122 班), and the deciding signal is the feed's
        # scheduled departure vs the run's dep0 - a train that left 大崎 at
        # 20:55 must land on the 20:55 run (dep_dev=0), not merely on the
        # passage-closest run (21:00 run may have a marginally smaller gap).
        for p, d in pas:
            dep_dev = abs(d - dep_feed) if dep_feed is not None else 0
            scored.append((dep_dev, abs(p - est), c, p, d))
    if not scored:
        return cands
    scored.sort(key=lambda x: (x[0], x[1]))
    # ambiguous only when the two best candidates are near-equal in BOTH
    # departure deviation and passage gap (8356/11011 share dep_dev=6 but
    # differ 3 vs 9 min passage → 8356 wins; two true same-pattern runs
    # would tie in both and stay ambiguous for the front-end to break)
    if (
        len(scored) >= 2
        and scored[1][0] - scored[0][0] <= 2
        and scored[1][1] - scored[0][1] <= 2
        and scored[0][1] <= tol
    ):
        return cands  # two runs nearly equally close -> ambiguous
    dep_dev, gap, cand, pmin, dep0 = scored[0]
    c2 = dict(cand)
    c2["dep_run"] = f"{pmin // 60:02d}:{pmin % 60:02d}"
    # suki 口径的发车分钟(与主站 train.depMin 同基准):by_key 的 dep 决胜
    # 用它,而不是 dokorail 的 scheduled.dep(字符串,前端 JS 相减变 NaN)
    c2["dep0"] = dep0
    return [c2]


def _safe_feed(name, thunk):
    """Run one feed defensively: any unexpected provider change (format
    drift, 5xx, schema swap) must cost that operator's data, not the whole
    bake run. Mirrors train_feed's per-operator fault tolerance for every
    self-contained source."""
    try:
        return thunk()
    except Exception as exc:  # noqa: BLE001 - survive any provider surprise
        print(f"feed {name}: {type(exc).__name__}: {str(exc)[:160]}")
        return None


def _fetch_feeds(feed_jobs, workers=FEED_FETCH_WORKERS):
    """(name, thunk) jobs -> [(name, payload)] fetched concurrently."""
    feeds = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        future = {ex.submit(_safe_feed, n, t): n for n, t in feed_jobs}
        for f in concurrent.futures.as_completed(future):
            feeds.append((future[f], f.result()))
    return feeds


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


def _source_lag_minutes(fetched_iso, source_time):
    """fetched_at - source_time in whole minutes, tolerating every provider
    timestamp dialect seen so far. Naive stamps are read as JST; an explicit
    GMT/Z suffix means UTC. Returns None when unparseable/missing."""
    import re as _re
    from datetime import datetime, timedelta, timezone

    if not source_time:
        return None
    jst = timezone(timedelta(hours=9))
    try:
        f_dt = datetime.fromisoformat(fetched_iso)
    except Exception:
        return None
    s_raw = str(source_time).strip()
    s_dt = None
    m = _re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日(\d{1,2})時(\d{1,2})分", s_raw)
    try:
        if m:
            y, mo, dd, hh, mi = map(int, m.groups())
            s_dt = datetime(y, mo, dd, hh, mi, tzinfo=jst)
        elif s_raw.endswith("GMT"):
            s_dt = datetime.strptime(s_raw, "%Y/%m/%d %H:%M:%S GMT").replace(
                tzinfo=timezone.utc
            )
        elif _re.match(r"^\d{14}$", s_raw):
            s_dt = datetime.strptime(s_raw, "%Y%m%d%H%M%S").replace(tzinfo=jst)
        elif "T" in s_raw:
            s_dt = datetime.fromisoformat(s_raw.replace("Z", "+00:00"))
            if s_dt.tzinfo is None:
                s_dt = s_dt.replace(tzinfo=jst)
        elif "/" in s_raw:
            s_dt = datetime.strptime(s_raw, "%Y/%m/%d %H:%M:%S").replace(tzinfo=jst)
    except Exception:
        return None
    if s_dt is None:
        return None
    return round((f_dt - s_dt).total_seconds() / 60)


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
        self.cal_cache = {}
        self.primary_cal = {}

    def _legs_from_blocks(self, railway, blocks):
        line = self.lines.get(railway)
        names = line["stations"] if line else []
        legs = []
        for block in blocks:
            if block[2] != railway:
                continue
            for number, stops, meta in block[5]:
                dates = meta.get("dates")
                if dates and self.today not in common.dated_runs(dates[1], dates[0]):
                    continue
                decoded = common.decode_stops(stops, names)
                legs.append(
                    {
                        "railway": railway,
                        "number": number,
                        "type": meta.get("type"),
                        "names": [n["ja"] for n in meta.get("names") or []],
                        "stops": decoded,
                        "dep": decoded[0]["dep"],
                        "arr": decoded[-1].get("arr") or decoded[-1]["dep"],
                    }
                )
        return legs

    def railway_legs_cal(self, railway, cal):
        """Legs from one specific calendar (cached per railway+calendar)."""
        key = (railway, cal)
        if key in self.cal_cache:
            return self.cal_cache[key]
        entry = self.index.get(railway)
        legs = []
        if entry and entry.get("bundle"):
            asset = entry["bundle"]["calendars"].get(cal)
            if asset:
                legs = self._legs_from_blocks(
                    railway, common.bundle_blocks(self.source.bundle(asset["url"]) or [])
                )
        self.cal_cache[key] = legs
        return legs

    def prefetch(self, railways, workers=8):
        """Eagerly download timetable bundles for every candidate railway so
        the sequential board walk (find_leg / railway_legs) hits the cache
        instead of doing dozens of serial network fetches - the old run's
        dominant cost (~4 min on GitHub runners, 9 min from slower hosts)."""
        jobs = []
        for rid in railways:
            entry = self.index.get(rid)
            if not entry or not entry.get("bundle"):
                continue
            for cal in self.calendar_candidates(entry, self.weekday, self.today):
                if (rid, cal) in self.cal_cache:
                    continue
                asset = entry["bundle"]["calendars"].get(cal)
                if asset:
                    jobs.append((rid, cal, asset["url"]))
        if not jobs:
            return

        def _load(item):
            rid, cal, url = item
            try:
                blocks = common.bundle_blocks(self.source.bundle(url) or [])
                return rid, cal, blocks
            except Exception as exc:  # noqa: BLE001 - one bundle must not abort
                print(f"prefetch {rid}/{cal}: {type(exc).__name__}: {str(exc)[:100]}")
                return rid, cal, None

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            for rid, cal, blocks in ex.map(_load, jobs):
                if blocks is not None:
                    self.cal_cache[(rid, cal)] = self._legs_from_blocks(rid, blocks)

    def railway_legs(self, railway):
        """Primary calendar legs (first candidate that has this railway's
        trains); further calendars stay available for find_leg fallback."""
        if railway in self.by_railway:
            return self.by_railway[railway]
        entry = self.index.get(railway)
        line = self.lines.get(railway)
        legs = []
        primary = None
        if entry and line and entry.get("bundle"):
            for cal in self.calendar_candidates(entry, self.weekday, self.today):
                legs = self.railway_legs_cal(railway, cal)
                if legs:
                    primary = cal
                    break
        self.by_railway[railway] = legs
        self.primary_cal[railway] = primary
        return legs

    def fallback_legs(self, railway):
        """Legs from the railway's calendars NOT loaded as primary - used
        when a feed train misses the primary pool (per-date runs like
        4809T live in Dated while the primary Weekday table is loaded)."""
        entry = self.index.get(railway)
        if not entry:
            return []
        primary = self.primary_cal.get(railway)
        legs = []
        for cal in self.calendar_candidates(entry, self.weekday, self.today):
            if cal == primary:
                continue
            legs.extend(self.railway_legs_cal(railway, cal))
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

    def find_leg(self, railway, number, op=None, railways=None, number_prefix=None, number_digits=None):
        """Best leg for a feed train, operator-scoped. `railways` lists the
        dokorail railways to search (self-contained operators like JR-Kyushu
        know their routes); with `number_prefix`, legs match by number
        prefix (feed gives bare "3222", bundles carry "3222M"); with
        `number_digits`, legs match on digit-equality ignoring the depot
        letter (JR-West guide "2724H" == dokorail "2724M"). Ties prefer the
        leg whose window brackets now, then the shortest suffix."""
        if railways:
            pools = [self.railway_legs(r) for r in railways]
        elif railway:
            pools = [self.railway_legs(railway)]
            if op:
                # every same-operator railway with a bundle - a train filed
                # under one line often runs through onto a sibling line
                # (4002D: feed says Kotoku, dokorail files it under
                # Tokushima), so the fallback pool must be complete
                same_op = []
                for rid, e in self.index.items():
                    if not rid.startswith(op + ".") or rid == railway:
                        continue
                    if (e.get("bundle") or {}).get("calendars"):
                        same_op += self.railway_legs(rid)
                pools.append(same_op)
        else:
            pools = []
        base = number_prefix or number or ""
        if not base and not number_digits:
            return None  # no number to match on; caller falls back to position matching

        def leg_digits(leg_number):
            core = leg_number.split(".", 1)[0]  # drop through-leg suffix
            return "".join(ch for ch in core if ch.isdigit())

        def match_pool(pool):
            if number_digits:
                cands = [l for l in pool if leg_digits(l["number"]) == number_digits]
                if cands:
                    # prefer a bundle number whose depot letter matches, then
                    # the closest time window
                    cands.sort(key=lambda l: (l["number"] != base, len(l["number"])))
                    return self._closest(cands)
                return None
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
        # calendar fallback: the train may live in a non-primary calendar of
        # the same railways (Dated runs like 4809T while Weekday is loaded)
        for r in railways or ([railway] if railway else []):
            fb = self.fallback_legs(r)
            if fb:
                hit = match_pool(fb)
                if hit:
                    return hit
        return None

    def _closest(self, legs):
        def window(leg):
            dep, arr = hhmm_to_min(leg["dep"]), hhmm_to_min(leg["arr"])
            return 0 if dep <= self.now_min <= arr else min(abs(self.now_min - dep), abs(self.now_min - arr))

        return sorted(legs, key=window)[0]

    def find_leg_by_position(self, railways, station, delay_min, tolerance=20, dest_hint=None, upcoming=None):
        """ID-less matching: the feed reports "at <station>, N min late" -
        return the leg whose timetable has it stopping at that station
        around (now - N). Stops AND times both participate (停站和时间基本
        吻合); ties resolve to the nearest scheduled passage. For number-less
        feeds (Hanshin-style slot boards) and as fallback when a feed number
        has no dokorail leg (e.g. Nankai extras). Returns (leg, gap_min).

        Direction disambiguation (both directions stop at the anchor at
        similar times): `upcoming` (ordered next stops from the feed) must
        appear in order in the leg - works across through-service splits,
        where no single leg contains both anchor and final destination.
        dest_hint then prefers legs that actually contain the destination."""
        if not station:
            return None, None
        target = norm_station_key(station)
        if not target:
            return None, None
        dest = norm_station_key(dest_hint or "") or None
        seq = [norm_station_key(s) for s in (upcoming or []) if s]
        pass_time = self.now_min - delay_min
        best, best_gap = None, None
        best_dir, best_dir_gap = None, None
        for rid in railways or []:
            for leg in self.railway_legs(rid):
                stop_names = [norm_station_key(s["station"]) for s in leg["stops"]]
                ordered = len(seq) >= 2 and self._is_ordered_subseq(seq, stop_names)
                if len(seq) >= 2 and not ordered:
                    continue  # wrong direction: its stops run the other way
                # destination must be the leg's terminus (mere membership is
                # symmetric - the opposite leg contains it as its origin)
                directed = ordered or (dest is not None and stop_names and dest == stop_names[-1])
                for idx, name in enumerate(stop_names):
                    if name != target:
                        continue
                    s = leg["stops"][idx]
                    arr = hhmm_to_min(s.get("arr") or s["dep"])
                    dep = hhmm_to_min(s["dep"])
                    gap = abs((arr + dep) / 2 - pass_time)
                    if directed:
                        if best_dir_gap is None or gap < best_dir_gap:
                            best_dir, best_dir_gap = leg, gap
                    if best_gap is None or gap < best_gap:
                        best, best_gap = leg, gap
                    break  # first occurrence of the station on this leg
        if best_dir is not None and best_dir_gap <= tolerance:
            return best_dir, round(best_dir_gap, 1)
        if best is not None and best_gap <= tolerance:
            return best, round(best_gap, 1)
        return None, None

    @staticmethod
    def _is_ordered_subseq(needle, hay):
        it = iter(hay)
        return all(any(s == h for h in it) for s in needle)


def bake(source, data_dir):
    lines = json.loads((Path(data_dir) / "lines.json").read_text())
    realtime_ops = sorted(lines["realtime_operators"])
    # Toei/Keikyu/Tobu/YokohamaMunicipal now come from ODPT (baker/odpt.py):
    # dokorail's own feeds for these 403 GitHub runner IPs via Cloudflare
    realtime_ops = [
        op for op in realtime_ops
        if op not in {"JR-West", "Toei", "Keikyu", "Tobu", "YokohamaMunicipal"}
    ]
    # The timetable index is a large JSON (~0.5 MB) that used to stall the
    # whole run for minutes on flaky networks; it is independent of the live
    # feeds, so download it concurrently instead of serially in front of them.
    _index_box = {}

    def _load_index():
        try:
            _index_box["v"] = source.timetable_index()["railways"]
        except Exception as exc:  # noqa: BLE001 - flaky CDN: degrade to no-legs
            print(f"timetable index unavailable ({exc}); baking without legs")
            _index_box["v"] = {}

    _index_thread = threading.Thread(target=_load_index, daemon=True)
    _index_thread.start()
    titles = title_index(lines["lines"])
    now = common.jst_now()
    today_iso = now.date().isoformat()
    # JR runs the holiday (Saturday/Sunday/Holiday) timetable on shukujitsu
    # days too - a Monday national holiday must NOT match against the weekday
    # timetable. Holidays collapse into the Sunday branch (SaturdayHoliday ->
    # Holiday -> Saturday order), which is what JR groups actually publish.
    wkd = now.weekday()
    if holidays.is_holiday_today(today_iso):
        wkd = 6
        print(f"calendar: {today_iso} is a Japanese national holiday -> Holiday timetable")
    else:
        print(f"calendar: JST {today_iso} weekday={wkd}")
    suki_index = suki.SukiIndex.load(data_dir)
    # the site's own timetable turns station-sequence candidates into
    # time-verified unique mappings (T.t = tmaps key, T.s[0] = dep minute)
    stt = suki_timetable.SukiTimetable.load(data_dir, wkd)
    if stt:
        print(f"suki timetable: {len(stt.runs)} keys from {stt.source_file}")

    board = []
    op_stats = {}
    # Feed jobs run concurrently (each hits a different site); per-operator
    # fault tolerance is enforced by _safe_feed on top of each source's own
    # internal try/except, so a provider format change costs one feed at most.
    feed_jobs = [(op, lambda op=op: source.train_feed(op)) for op in realtime_ops]
    # Operators with their own position services (suspended/403 -> None and
    # silently skipped, auto-join on recovery):
    feed_jobs += [
        ("JR-Shikoku", source.jr_shikoku_feed),
        ("JR-Hokkaido", source.jr_hokkaido_feed),
        ("JR-Kyushu", source.jr_kyushu_feed),
        ("JR-East", source.jr_east_dokotrain_feed),
        ("JR-East", source.jr_east_app_feed),
        ("Nankai", source.nankai_feed),
        ("Nishitetsu", source.nishitetsu_feed),
        ("JR-Central", source.jr_central_feed),
        ("Keihan", source.keihan_feed),
        ("Hanshin", source.hanshin_feed),
        ("Kintetsu", source.kintetsu_feed),
        ("JR-West", source.jr_west_guide_feed),
        # ODPT operators (Toei keyless; Tobu/YokohamaMunicipal keyed). Keikyu has
        # its own keyless web API (baker/keikyu.py)
        ("Keikyu", source.keikyu_feed),
        ("Keisei", source.keisei_feed),
        ("TsukubaExpress", source.tx_feed),
        ("Tobu-SSR", source.tobuspapp_feed),
        ("Tokyu-tid", source.tokyu_feed),
        ("Seibu", source.seibu_feed),
    ]
    _odpt_ops = ["Toei", "Tobu", "YokohamaMunicipal"]
    if os.environ.get("ODPT_KEY", "").strip():
        # with a consumer key the keyed API covers the rest of the 東京圏
        # cluster too (東京メトロ/東急/相鉄系/埼玉/モノレール/りんかい等);
        # without it those operators answer empty, so they stay off the list
        _odpt_ops += [
            "JR-East",
            "TokyoMetro", "Tokyu", "KantoRailway", "SaitamaRailway",
            "SaitamaTransit", "TWR", "TokyoMonorail", "Rinkai",
            "Yurikamome", "Minatomirai", "Shibayama", "ToyoRapid",
        ]
    feed_jobs += [(_op, lambda op=_op: source.odpt_feed(op)) for _op in _odpt_ops]
    feeds = _fetch_feeds(feed_jobs)
    # join the concurrent index download; either way we proceed (degraded to
    # no-legs when the CDN refused it)
    _index_thread.join()
    index = _index_box.get("v", {})
    store = LegStore(source, index, lines["lines"], today_iso, wkd, now.hour * 60 + now.minute)
    # Prewarm: collect every railway that may need timetable legs and download
    # the bundles in parallel, so the sequential board walk below hits the
    # LegStore cache instead of serial network fetches (the run's old cost).
    cand_rails = set()
    for _op, _payload in feeds:
        if not _payload or not _payload.get("ok"):
            continue
        for _t in _payload.get("body") or []:
            if not isinstance(_t, dict):
                continue
            _rw = (_t.get("odpt:railway") or "").replace("odpt.Railway:", "").replace("odpt:Railway:", "")
            if _rw:
                cand_rails.add(_rw)
            for _part in (_t.get("_line_ja") or "").split("・"):
                cand_rails.update(titles.get(_part, []))
    store.prefetch(cand_rails)
    claimed_suki = {}  # key -> train number, global uniqueness: one suki train per bake
    for op, payload in feeds:
        if not payload or not payload.get("ok"):
            # feeds complete in arbitrary order (as_completed); several feeds
            # may share one operator name (JR-East: dokorail train feed +
            # dokotrain + cloudrail app). An unavailable feed must not
            # overwrite a sibling's successful stats written moments earlier.
            if "trains" not in op_stats.get(op, {}):
                op_stats[op] = {"feed": "unavailable"}
                print(f"feed unavailable: {op}")
            continue
        body = payload.get("body") or []
        delayed = 0
        for t in body:
            if not isinstance(t, dict):
                continue
            delay = t.get("odpt:delay")
            # providers occasionally swap numeric delay for a text field
            # (Keihan's delayMinutes became "6分遅れ"); one bad record must
            # not abort the whole run
            if not isinstance(delay, (int, float)) or delay is None or delay < DELAY_THRESHOLD_SECONDS:
                continue
            delayed += 1
            railway = (t.get("odpt:railway") or "").replace("odpt.Railway:", "").replace("odpt:Railway:", "")
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
            if t.get("_nickname"):
                entry["name"] = t["_nickname"]
            if t.get("_dest"):
                entry["to"] = t["_dest"]
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
                    # 東海道本線 exists at three JRs, numbers can collide.
                    # Composite names (白新線・羽越本線) match each part.
                    raw_title = t["_line_ja"]
                    # Kintetsu short names (奈良線) vs dokorail full titles
                    # (近畿日本鉄道奈良線): try the short form, its 名-grown
                    # variant and composites (白新線・羽越本線)
                    candidates = [raw_title, "近畿日本鉄道" + raw_title, "近鉄" + raw_title]
                    parts = [raw_title] + raw_title.split("・")
                    for part in parts:
                        candidates += [part, "近畿日本鉄道" + part, "近鉄" + part]
                    railways_hint = []
                    for part in candidates:
                        railways_hint += [
                            r for r in titles.get(part, [])
                            if r.startswith(op + ".") and r not in railways_hint
                        ]
                    # cross-operator through services (瀬戸大橋線: the JR-West
                    # feed reports a train whose leg lives under JR-Shikoku)
                    # - second pass over every railway sharing the title
                    railways_hint += [
                        r for r in titles.get(parts[0], [])
                        if r not in railways_hint
                    ]
                for rid in railways_hint or []:
                    store.railway_legs(rid)
                leg = store.find_leg(
                    railway,
                    t.get("odpt:trainNumber"),
                    op=op,
                    railways=railways_hint,
                    number_prefix=t.get("_number_prefix"),
                    number_digits=t.get("_number_digits"),
                )
                match_method = "number" if leg else None
                pos_gap = None
                if not leg and t.get("_station"):
                    # position-time fallback: the train reports where it is
                    # and how late - find the timetable leg that fits
                    leg, pos_gap = store.find_leg_by_position(
                        railways_hint,
                        t["_station"],
                        round(delay / 60),
                        dest_hint=t.get("_dest"),
                        upcoming=t.get("_upcoming"),
                    )
                    match_method = "position" if leg else None
                if not leg and (t.get("_station_keys") or (t.get("_station") and t.get("_line_ja"))):
                    # last resort - suki-direct: match straight against suki
                    # trains on the line that contain the reported station;
                    # direction from the feed number's parity (odd = 下り)
                    # checked against the entry's terminal order, stop-count
                    # bucket from type. Runs even when dokorail legs exist: a
                    # leg that fails number/position matching must still fall
                    # back here instead of being dropped (by_key only accepts
                    # a unique suki-direct hit, so ambiguity is still
                    # dropped - 宁漏不误报). Multi-fragment position text
                    # ("栗林～屋島") tries each fragment in order
                    # (jrshikoku's _station_keys).
                    num = str(t.get("odpt:trainNumber") or "")
                    direction = None
                    if num.isdigit():
                        direction = "down" if int(num) % 2 else "up"
                    # JR-Central 新干线: 报站站码决定的线路 ≠ 车实际线路
                    # (753 报站新大阪但終点鹿児島中央、2456 只 tag 山陽),
                    # 跨東海道/山陽/九州全部新干线池合并候选;其他线路单池
                    lines_pool = (["東海道新幹線", "山陽新幹線", "九州新幹線"]
                                  if t.get("_train_kind") is not None
                                  else (t.get("_suki_pools") or [t["_line_ja"]]))
                    direct = None
                    for station in (t.get("_station_keys") or [t.get("_station")]):
                        if not station:
                            continue
                        merged = []
                        # top 须覆盖全池: 候选按站数升序截断, 12 站全程车
                        # (7237/193) 若 top 太小会被小站数车挤出, 导致宁漏
                        for ln in lines_pool:
                            merged += suki_index.match_direct(
                                ln, station, direction,
                                entry.get("type"), top=256,
                            ) or []
                        seen = set()
                        direct = []
                        for c in merged:
                            if c["key"] not in seen:
                                seen.add(c["key"])
                                direct.append(c)
                        if direct:
                            break
                    if direct:
                            entry["suki"] = direct
                            entry["match"] = "suki-direct"
                            # 時刻消歧：无 dokorail 时刻表的线路（新幹線等）
                            # 用 suki 時刻表把多候選收斂為唯一班次並補
                            # scheduled（主站打標需要 dep/origin/stops）；
                            # JR-Central 官方発車順序可用時，以官方表定時刻
                            # 為基準 + 官方停站序列做站序一致性校驗；
                            # 消歧不出 → 保持多候選，by_key 只信單候選 → 不映射
                            official = None
                            if stt and (len(direct) > 1 or t.get("_official")):
                                # feeds may ship their own official timetable
                                # (jreast_app: cloudrail planArrival + remaining
                                # stops; only used when the feed is sure of it)
                                official = t.get("_official")
                                if not official and t.get("_bound") is not None and t.get("_station_code") and t.get("_train_kind"):
                                    try:
                                        official = jrcentral.fetch_shinkansen_departure(
                                            source, t["_station_code"], t["_bound"],
                                            t.get("odpt:trainNumber"), t["_train_kind"],
                                        )
                                    except Exception:  # noqa: BLE001 - degrade to now-delay est
                                        official = None
                                resolved = resolve_direct_by_time(
                                    stt, suki_index, direct,
                                    t["_station"], now, round(delay / 60),
                                    entry.get("type"), official=official,
                                    dest=t.get("_dest"),
                                )
                                if resolved:
                                    rk = str(resolved["cand"]["key"])
                                    # global uniqueness: a suki train can only be
                                    # occupied by one live train per bake (loop
                                    # lines like 山手線 would otherwise map every
                                    # passing train to the same loop entry)
                                    if rk in claimed_suki:
                                        continue
                                    claimed_suki[rk] = t.get("number", "?")
                                    entry["suki"] = [resolved["cand"]]
                                    entry["scheduled"] = resolved["scheduled"]
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
                if not cands and suki_line and "・" in suki_line:
                    # composite dokotrain groups (中央本線・篠ノ井線): suki tags
                    # trains with the individual line names - retry per part
                    for part in suki_line.split("・"):
                        cands = suki_index.match(stations, part, railway=railway)
                        if cands:
                            break
                # 环线线路(山手線等):match() 默认 top=3 按站序差截断,会把
                # 双圈高频 key(11011/11016,extra=30)挤出候选池,只剩单圈
                # 低频 key(8356/10245)与部分环(10891/10594)。但主站时刻表
                # 里的环线列车挂在双圈高频 key 下,收敛到错误 key 会让主站
                # for('11011') 查不到 → 地图无高亮。检测到候选含环线
                # (start==end)key 时放大 top 到全池,让双圈 key 进入,
                # 再由 _collapse_by_time 按报告站 passage 收敛到唯一班次。
                if cands and any(
                    (
                        stops_n := suki_index._stops_norm.get(str(c["key"]))
                    ) is not None
                    and len(stops_n) > 1
                    and stops_n[0] == stops_n[-1]
                    for c in cands
                ):
                    cands = suki_index.match(
                        stations, suki_line, railway=railway, top=256
                    )
                if cands and stt:
                    # time-verify: keep only keys that actually run at this
                    # train's departure minute today; through/mid-route
                    # trains (key origin ≠ leg origin) are checked at their
                    # passage time at the leg's origin station. by_key below
                    # only trusts verified entries (宁漏不误报).
                    dep_hhmm = entry.get("scheduled", {}).get("dep")
                    verified, rest = _verify_passage(
                        suki_index, stt, cands, stations[0], dep_hhmm
                    )
                    if verified:
                        cands = verified + rest
                if cands and stt and len(cands) > 1:
                    # station-sequence ties (山手線/通勤线同号多圈) that survive
                    # verify are collapsed by the reported station's passage
                    # time ≈ now − delay: same moment, same path, one train.
                    report_station = t.get("_station") or entry.get("from")
                    if report_station:
                        collapsed = _collapse_by_time(
                            stt, suki_index, cands, report_station,
                            now, round(delay / 60), leg_stops=stations,
                            dep_feed=_hhmm_min(dep_hhmm),
                        )
                        if collapsed and len(collapsed) == 1:
                            cands = collapsed
                if cands:
                    entry["suki"] = cands
            board.append(entry)
        # multiple feeds may share an operator name (JR-East: dokotrain +
        # cloudrail app) - accumulate per-feed stats instead of overwriting
        prev = op_stats.get(op)
        if not isinstance(prev, dict):
            prev = {}
        op_stats[op] = {
            "fetched_at": payload.get("fetchedAt") or prev.get("fetched_at"),
            "source_time": payload.get("source_time") or prev.get("source_time"),
            "trains": len(body) + (prev.get("trains") or 0),
            "delayed": delayed + (prev.get("delayed") or 0),
        }

    board.sort(key=lambda e: (-e["delay_s"], e["railway"], e["number"] or ""))
    by_key = {}
    for e in board:
        cand_items = e.get("suki") or []
        # 同一列车多条时间验证通过的候选:只保留与表定时刻差最小的
        # 一条;差并列(同刻多班,如予讃線 15:28 两班)无法确定真身→宁漏
        verified = [c for c in cand_items if "dep_run" in c]
        if len(verified) > 1:
            dep_min = _hhmm_min(e.get("scheduled", {}).get("dep"))
            if dep_min is not None:
                scored = [
                    (abs(_hhmm_min(c["dep_run"]) - dep_min), c)
                    for c in verified
                    if _hhmm_min(c["dep_run"]) is not None
                ]
                if scored:
                    best_delta = min(s for s, _ in scored)
                    ties = [c for s, c in scored if s == best_delta]
                    cand_items = [ties[0]] if len(ties) == 1 else []
            e["suki"] = cand_items
        for c in cand_items:
            # strong matches only for the flat lookup table: exact ordered
            # containment, or off by at most one stop (stopping-pattern drift
            # between the datasets); suki-direct candidates are trusted only
            # as a unique hit (no sequence evidence behind them)
            if c.get("direct"):
                if len(e.get("suki") or []) != 1 or e.get("match") != "suki-direct":
                    continue
            else:
                if not (c["subseq"] or c.get("fuzzy", 99) <= 1):
                    continue
                # 在来線:只写时间验证通过(passage 基准)的候选。未验证的
                # 候选只是站序相似的别时刻班次——写入会把晚点误标到别的
                # 列车(204M 曾被写到 21:40 夜车/6:16 早车)。宁漏不误报。
                if "dep_run" not in c:
                    continue
            slot = by_key.setdefault(
                c["key"],
                {
                    "key": c["key"],
                    "delay_min": e["delay_min"],
                    "train": e["number"],
                    "line": e["line"],
                    "type": e["type"],
                    # 数字发车分钟(suki dep0 口径),主站 delay-tag.js 用
                    # |a.dep - train.depMin| 决胜;字符串 HH:MM 在 JS 相减
                    # 得 NaN,历史 bug:山手線全被 ambiguous 且无法决胜。
                    "dep": c.get("dep0")
                    if c.get("dep0") is not None
                    else _hhmm_min(e.get("scheduled", {}).get("dep")),
                    "score": c["overlap"],
                    "fuzzy": 0 if c.get("subseq") else c.get("fuzzy", 0),
                    "ambiguous": False,
                    "alternatives": [],
                },
            )
            if slot["train"] != e["number"]:
                slot["ambiguous"] = True
                slot["alternatives"].append(
                    {
                        "train": e["number"],
                        "line": e["line"],
                        "delay_min": e["delay_min"],
                        "dep": c.get("dep0")
                        if c.get("dep0") is not None
                        else _hhmm_min(e.get("scheduled", {}).get("dep")),
                    }
                )
                # same stopping pattern, several delayed services at once:
                # surface the worst delay (both directions must stay visible on
                # the map; the front-end breaks the tie by dep anyway)
                slot["delay_min"] = max(slot["delay_min"], e["delay_min"])
    # freshness: for each source, how stale the provider's data was when we
    # fetched it (fetched_at minus the provider's own latest timestamp).
    freshness = {}
    for op, st in op_stats.items():
        fetched = st.get("fetched_at")
        if not fetched:
            continue
        lag = _source_lag_minutes(fetched, st.get("source_time"))
        freshness[op] = {
            "fetched_at": fetched[:19],
            "source_time": st.get("source_time"),
            "lag_minutes": lag,
        }
    return {
        "version": 1,
        "generated_at": now.isoformat(),
        "date": now.date().isoformat(),
        "delayed_count": len(board),
        "matched_suki_count": len(by_key),
        "freshness": freshness,
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

    # sukijr.json: the flat {tmaps_key: delay_minutes} lookup for the site.
    # Unambiguous strong matches map directly. Ambiguous keys (same stopping
    # pattern, several delayed services at once) are also included now -
    # delay_min is the worst in the group, and the front-end breaks the dep
    # tie; dropping them whole made whole directions disappear from the map.
    sukijr = {k: v["delay_min"] for k, v in payload["by_suki_key"].items()}
    sukijr_out = {
        "version": 1,
        "generated_at": payload["generated_at"],
        "date": payload["date"],
        "freshness": payload.get("freshness", {}),
        "delay_min": sukijr,
    }
    (Path(args.data_dir) / "sukijr.json").write_text(
        json.dumps(sukijr_out, ensure_ascii=False, separators=(",", ":"))
    )
    with_legs = sum(1 for t in payload["trains"] if t.get("scheduled"))
    with_suki = sum(1 for t in payload["trains"] if t.get("suki"))
    print(
        f"delays: {payload['delayed_count']} delayed, "
        f"{with_legs} with dokorail leg, {with_suki} mapped to suki keys "
        f"({payload['matched_suki_count']} unique keys, "
        f"{len(sukijr)} in sukijr.json) -> {out}"
    )


if __name__ == "__main__":
    main()
