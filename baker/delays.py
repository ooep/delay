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
import unicodedata
from pathlib import Path

from . import common, holidays, suki, suki_timetable, jrcentral
from . import tokyometro_firestore

DELAY_THRESHOLD_SECONDS = 60
# 同一条线常有几十种停站模式共享同一前缀(直通/快速/各停):top=3 会把
# 真正的长 pattern 挤出候选(例:中野→東葉勝田台 排在 中野→西船橋 之后),
# 后续就只能收敛到近似模式。放宽到 32 只是多进候选,时间验证、终点、
# 站序与唯一性仍是硬门槛(宁漏不误报)。
MATCH_TOP = 32
# Parallel feed fetch: sources are independent websites, so we fetch them
# concurrently (the previous serial walk took ~4 min, dominated by network
# waits). Each feed's internal request order stays untouched - JR-Kyushu's
# WAF throttling, doko-train's line scans etc. remain serial inside the feed.
FEED_FETCH_WORKERS = 12
# suki-direct 时刻消歧：报告站过站时刻与 (now - delay) 的容差（分钟）。
# 新干线等无 dokorail 时刻表的线路，用 suki 时刻表把多候选收敛为唯一班次；
# 收敛必须严格（窗口内唯一趟 + 唯一 key + 车种特征吻合），否则宁漏不误报。
DIRECT_TIME_TOLERANCE_MIN = 3
# 单候选模式的观测滞后容差:报站只命中一个停站模式时(车种/终点/方向已
# 筛过)，feed 页面刷新滞后会让 now-delay 比表定晚几分钟。只有同一模式
# 前后班次相距足够远、最近那趟仍然唯一时，才允许超出上面的 ±3 分钟窗。
SINGLETON_STALE_TOLERANCE_MIN = 6
SINGLETON_RUN_SEPARATION_MIN = 8
# feed 自带班次号的线路(函館市電 ICAS: "系统_發車HHMM_…")：用班次号中段
# 的表定始发分钟钉车，再用车辆当前站该班次的表定通过时刻与 now-delay
# 做一致性闸门。偏差超限说明班次号/时刻表已漂移，宁可不钉。
TRIP_ID_TIME_TOLERANCE_MIN = 6


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


def _stops_consistent(stops_n, report_station, official, max_extra=1):
    """候选 key 的停站序列必须与官方発車順序（departure_info）一致：
    报站之后的候选停站须以官方后续停站为前缀（允许候选继续延伸，如
    さくら 753 官方列表到博多、候选 2456 延伸到鹿児島中央），且官方
    终点站必须落在候选站序中。站名兼容県后缀（小倉(福岡県) ↔ 小倉）。

    max_extra: 候选在官方列表之后还允许延伸几站。默认 1 保持原有严格度；
    舒适度(comfort)对贯通车传 None —— cloudrail 只发布 JR 段内的后续停站，
    埼京線→新木場、常磐線各停→小田急 的剩余站在候选里必然超出官方列表，
    而前缀 + 终点站(dest) 两条硬约束已经锁定同一列车。"""
    off_stations = [
        suki.norm_station(s) for s in (official.get("stations") or []) if s
    ]
    # 官方的 station/terminal 可能带括号后缀或行先装饰（富山的行先是
    # 「南富山駅前（富山駅 経由）」、渥美線 GTFS 带「(渥美線)」），
    # 候选站序这边已经是 norm_station 过的裸名；不归一化会把整条线的
    # official 判成不一致，永远钉不上班次。
    terminal = suki.norm_station(official.get("terminal"))
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
    if len(tail) < n_off or (max_extra is not None and len(tail) > n_off + max_extra):
        return False
    for i, off in enumerate(off_stations):
        cand = tail[i]
        if cand != off and not (cand.startswith(off) and cand[len(off):].endswith(("都", "道", "府", "県"))):
            return False
    if len(tail) > n_off and terminal:
        # 多出的延伸必须落在官方终点站上(或带県后缀变体)
        if not (tail[-1].startswith(terminal) and tail[-1][len(terminal):].endswith(("都", "道", "府", "県"))
                or tail[-1] == terminal):
            return False
    if terminal:
        ok = terminal in stops_n or any(
            s.startswith(terminal) and s[len(terminal):].endswith(("都", "道", "府", "県"))
            for s in stops_n
        )
        if not ok:
            return False
    return True


def _widen_official_candidates(suki_index, cands, station, official):
    """Add every key whose stop order carries the feed's official station
    sequence, not just the keys tagged with the feed's line label.

    Feeds name one line per service (埼京線・川越線), while suki tags the same
    physical run with several (東北本線/赤羽線/りんかい線), so the line-scoped
    pool misses the key and the row is dropped even though the feed ships its
    own downstream stop list. The official sequence + terminal is the identity
    evidence here - a key can only join when `_stops_consistent` accepts it -
    and this is only tried after the scoped pool has already failed."""
    seen = {str(c["key"]) for c in cands}
    want = suki.norm_station(station)
    out = list(cands)
    for k, stops_n in suki_index._stops_norm.items():
        if k in seen or want not in stops_n:
            continue
        if not _stops_consistent(stops_n, want, official, None):
            continue
        out.append({"key": k, "direct": True, "overlap": 1.0, "size": len(stops_n)})
    out.sort(key=lambda c: (c["size"], suki.key_order(c["key"])))
    return out


def _hhmm_min(hhmm):
    """"HH:MM" (hours may exceed 23) -> minutes from midnight, or None."""
    if not hhmm:
        return None
    try:
        h, m = str(hhmm).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


# 近鉄 live feed 用短站名(奈良/難波/名古屋/上本町), tmaps 用带社名前缀的
# 站名。不别名则 `_finalize_candidates` 的终点收窄永远落空, 同方向所有
# 停站模式按站数打平、整行被弃。只对 Kintetsu 生效: JR 奈良/名古屋 是
# 同名异站, 全局别名会把两家钉串。
KINTETSU_DEST_ALIASES = {
    "奈良": "近鉄奈良",
    "名古屋": "近鉄名古屋",
    "難波": "大阪難波",
    "上本町": "大阪上本町",
}


def _feed_dest(op, t):
    dest = t.get("_dest")
    if dest and op == "Kintetsu":
        return KINTETSU_DEST_ALIASES.get(dest, dest)
    return dest


def _direct_candidates(suki_index, lines, station, direction, train_type, railway):
    """match_direct over several suki pools, deduped by key (top must cover
    the whole pool: candidates are sorted by stop count, so a 12-stop
    through train would otherwise be pushed out by short patterns)."""
    seen, out, merged = set(), [], []
    for ln in sorted(lines):
        merged += suki_index.match_direct(
            ln, station, direction, train_type, top=256, railway=railway) or []
    for c in merged:
        if c["key"] not in seen:
            seen.add(c["key"])
            out.append(c)
    # `lines` is a set: without a total order the payload's suki list (and
    # resolve's tie-breaks) would change with PYTHONHASHSEED.
    out.sort(key=lambda c: (c["size"], suki.key_order(c["key"])))
    return out


def _match_candidates(suki_index, stations, line, railway, top):
    """suki_index.match() with composite-line fallback (中央本線・篠ノ井線).

    suki tags trains with individual line names, so a composite feed title
    has no pool of its own. Parts are *merged*, not first-hit-wins: a leg on
    the second line (飯山線 十日町→森宮野原) matches a through pattern tagged
    with both lines (4224 → 長野, extra=20) under the first part, which hid
    the exact 9-stop pattern under the second (3583, extra=0). The merged
    list uses match()'s own ranking so exact patterns surface first."""
    cs = suki_index.match(stations, line, railway=railway, top=top)
    if cs or not line or "・" not in line:
        return cs
    merged, seen = [], set()
    for part in line.split("・"):
        for c in suki_index.match(stations, part, railway=railway, top=top):
            k = str(c["key"])
            if k in seen:
                continue
            seen.add(k)
            merged.append(c)
    merged.sort(key=lambda c: (c["fuzzy"], -c["overlap"], abs(c["extra"]),
                              suki.key_order(c["key"])))
    return merged


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
        gap, pmin, dep0 = min((abs(p - dep_min), p, d) for p, d in pas)
        if gap <= tol:
            c2 = dict(c)
            c2["dep_run"] = f"{pmin // 60:02d}:{pmin % 60:02d}"
            # 班次的始发发车分钟(与 tmap T[].s[0] 同口径):站点按它回查
            # 班次;对中途起算的车段(dokorail leg 起点≠key 首站)它就是
            # 主站时刻表里该趟车的发车分钟
            c2["dep0"] = dep0
            c2["gap"] = gap
            verified.append(c2)
        else:
            rest.append(c)
    return verified, rest


def _same_station(a, b):
    if not a or not b:
        return False
    return (
        a == b
        or (a.startswith(b) and a[len(b):].endswith(("都", "道", "府", "県")))
        or (b.startswith(a) and b[len(a):].endswith(("都", "道", "府", "県")))
    )


def _serves_station(fr, stops_n):
    """True when the candidate's stop list serves the reported `from`.
    Spelling is compared with the NFKC fold because _align_from writes the
    tmap spelling back into `from` afterwards; section text (繁藤～新改)
    passes through its fragments."""
    if not stops_n:
        return False
    want = {_fold_norm(fr)}
    want.update(
        _fold_norm(p.strip()) for p in re.split(r"[~～]", str(fr)) if p.strip()
    )
    want.discard("")
    return any(_fold_norm(s) in want for s in stops_n)


_PSEUDO_DEST = ("直通", "方面", "経由", "環状")
# 副站名括注:norm_station 只剥 ()/（), feed 与 tmap 还用〈〉(明治神宮前〈原宿〉)
_ANGLE_PAREN = re.compile(r"\s*〈.*$")


def _norm_station_label(name):
    """norm_station with the feed's decorative suffix stripped (好摩行)."""
    return suki.norm_station(re.sub(r"行$", "", str(name or "").strip()))


def _stop_indexes(name, stops_n):
    """Indexes of a reported station on a candidate's normalized stop list.
    Accepts the prefecture suffix tmap appends and a terminal that wears a
    feed-side line prefix (京王線新宿 -> 新宿 at the end of a Keio run)."""
    want = _norm_station_label(name)
    if not want or not stops_n:
        return []
    out = []
    for i, s in enumerate(stops_n):
        if s == want or (
            s.startswith(want) and s[len(want):].endswith(("都", "道", "府", "県"))
        ):
            out.append(i)
        elif (
            i in (0, len(stops_n) - 1)
            and want.endswith(s)
            and 0 < len(want) - len(s) <= 6
        ):
            out.append(i)
    return out


def _from_indexes(fr, stops_n):
    """All indexes of a reported origin; section text (繁藤～新改) uses its
    fragments so a section report is not rejected for spelling."""
    out = []
    for part in re.split(r"[~～]", str(fr or "")):
        out.extend(_stop_indexes(part, stops_n))
    return sorted(set(out))


def _runs_whole_leg(stops_n, leg_stops):
    """True when the candidate key visits every stop of the leg in order
    (not necessarily back-to-back: the key may model a longer through run
    around the leg). A leg from another section or the reverse direction
    never fits, which is what makes this a usable identity test."""
    it = iter(stops_n)
    return all(any(s == want for s in it) for want in leg_stops)


def _pin_by_endpoints(entry, suki_index, stt):
    """Last-resort pin for rows whose leg stops describe another segment or
    the reverse direction (あずさ 甲府→松本 carrying a 甲府→高尾 leg): keep
    runs that serve the reported from→to in order and whose passage at the
    leg origin matches scheduled.dep. Exactly one (key, dep0) hit wins;
    anything less stays unpinned (宁漏不误报). Only runs for empty suki --
    a stop-pattern pin always outranks an endpoint-only one. A missing `to`
    is filled from the leg's own terminus, which then demands an exact
    departure match (no slack).

    On a busy through-service corridor (千代田線, 常磐線各停) several
    patterns share the whole reported from→to and pass the minute window at
    the leg origin, because their keys differ only past the reported `to`
    (北綾瀬 vs 柏/我孫子/取手). The leg's own stop sequence separates them:
    only keys that run the *entire* leg stop list in order can be the same
    train, which also throws out reverse/wrong-section legs by construction.
    When several still fit, the origin passage must be strictly closer
    (within 1 min) - otherwise the row stays unpinned."""
    if not stt:
        return
    to = entry.get("to")
    fr = entry.get("from")
    sched = entry.get("scheduled") or {}
    if not fr:
        return
    if not to or any(x in str(to) for x in _PSEUDO_DEST):
        # 终点缺失时用腿自己的终点当终点，但此时起点时刻必须整点吻合
        stops = sched.get("stops") or []
        if len(stops) < 2:
            return
        dest = _norm_station_label(stops[-1])
        tol = 0
    else:
        dest = _norm_station_label(to)
        tol = 5
    origin = _norm_station_label(sched.get("origin"))
    dep_min = _hhmm_min(sched.get("dep"))
    if not dest or not origin or dep_min is None or dest == _norm_station_label(fr):
        return
    leg_stops = [
        _norm_station_label(_ANGLE_PAREN.sub("", s))
        for s in (sched.get("stops") or [])
    ]
    if len(leg_stops) < 3:
        leg_stops = []
    hits = {}
    for key in suki_index._stops_norm:
        stops_n = suki_index._stops_norm[key]
        oi = _stop_indexes(origin, stops_n)
        if not oi or oi[0] >= len(stops_n) - 1:
            continue  # 起点即终点站时那里只有到达时刻,拿来对发车必错
        fi = _from_indexes(fr, stops_n)
        if not fi:
            continue
        ti = _stop_indexes(dest, stops_n)
        if not ti or ti[-1] <= fi[0]:
            continue
        times = stt.stop_times(key, stops_n, stops_n[oi[0]])
        if not times:
            continue
        gap, arr, _dep, dep0 = min(
            (abs(d - dep_min), a, d, d0) for a, d, d0 in times
        )
        if gap <= tol:
            hits.setdefault((str(key), dep0), (gap, str(key), arr, dep0, stops_n))
    vals = list(hits.values())
    if len(vals) > 1 and leg_stops:
        full = [h for h in vals if _runs_whole_leg(h[4], leg_stops)]
        if full:
            vals = full
            if len(vals) > 1:
                gmin = min(h[0] for h in vals)
                vals = [h for h in vals if h[0] == gmin] if gmin <= 1 else []
            # 结构唯一但时刻明显更远:那是 tmap 把直通区间截短后留下的假
            # 唯一(常磐線各停 我孫子 终到 vs 取手,车段却到取手)。时刻更
            # 近的别家班次与它矛盾,宁可不钉。
            if len(vals) == 1 and vals[0][0] > min(
                3, max(1, min(h[0] for h in hits.values()))
            ):
                return
    if len(vals) != 1:
        return
    gap, key, arr, dep0, stops_n = vals[0]
    entry["suki"] = [
        {
            "key": key,
            "subseq": True,
            "fuzzy": 0,
            "overlap": 1.0,
            "extra": len(stops_n) - 2,
            "size": len(stops_n),
            "dep_run": f"{arr // 60:02d}:{arr % 60:02d}",
            "dep0": dep0,
            "gap": gap,
            "endpoint": True,
        }
    ]


def _run_serves_pair(suki_index, a, b):
    """True when *some* tmap run visits `a` then `b` in that order. Tells
    "the destination is modeled as another train" (→ this pin is another
    train) apart from "the feed's destination lies past tmap's run" (through
    service boundary, where the pinned run is still the same train)."""
    if not a or not b:
        return False
    cache = suki_index.__dict__.setdefault("_pair_served", {})
    hit = cache.get((a, b))
    if hit is None:
        hit = False
        for stops in suki_index._stops_norm.values():
            try:
                i = stops.index(a)
            except ValueError:
                continue
            if b in stops[i + 1:]:
                hit = True
                break
        cache[(a, b)] = hit
    return hit


def _finalize_candidates(entry, stt, suki_index):
    """Filter and rank entry["suki"] so element 0 is the strongest evidence.
    The site consumes only suki[0]; leaving a reverse-direction or
    wrong-terminal pattern first (same line prefix, e.g. 中野→西船橋 vs
    中野→東葉勝田台) highlights another train. Filters are all hard:
    station order (subseq/fuzzy<=1), reported origin, destination
    reachability (direction + a tmap run that actually serves from→to),
    reported terminal, today's passage at the leg origin, then origin dep0
    vs scheduled dep. A full tie between two distinct keys stays ambiguous
    and is dropped (宁漏不误报)."""
    items = entry.get("suki") or []
    if not items:
        return
    direct = entry.get("match") == "suki-direct"
    if not direct:
        # 站点直接读 suki[0](不走 by_key 的强度门槛),这里必须同样只留
        # 强候选:无精确包含且站序差 >1 的模式是"看着像"的别列车,留着
        # 会被站点按发车分钟钉上去(羽越本線 561M fuzzy=3 实测)。宁漏。
        items = [c for c in items if c.get("subseq") or c.get("fuzzy", 99) <= 1]
    # 站点还用 `from` 是否落在 stop list 里做最后一道硬门槛;一条不服务
    # 报告站的模式要么被站点丢弃(白占 suki[0]),要么在消费方跳过该门槛
    # 时贴到别列车上(西武 3 池袋→西武秩父 特急被钉给停秋津的各停)。
    # 站序/终点/时刻验证都过不了这一关的,在这里就弃掉。
    if entry.get("from"):
        items = [
            c for c in items
            if _serves_station(
                entry["from"], suki_index._stops_norm.get(str(c.get("key"))) or []
            )
        ]
    dest = _norm_station_label(entry.get("to")) if entry.get("to") else None
    if dest and not any(x in str(entry.get("to")) for x in _PSEUDO_DEST):
        # 站点展示的是「这班车」的晚点;班次的行车方向必须与乘客报告一致。
        # 反向(终点在起点之前,近鉄 桃園→五十鈴川 曾贴到 五十鈴川→近鉄名古屋)
        # 一律弃;`to` 不在本班停站表里时,只有 tmap 没有任何 from→to 的班次
        # 才当作「越出 tmap 建模范围的通直」(西武秩父方向)保留,否则说明
        # 目的地本有别的班次可走、这里抓错了车(和光市→森林公園 贴到
        # 川越市→新木場)。宁漏不误报。
        leg_from = {
            _norm_station_label(p)
            for p in re.split(r"[~～]", str(entry.get("from") or ""))
            if p.strip()
        }
        keep = []
        for c in items:
            stops_n = suki_index._stops_norm.get(str(c.get("key"))) or []
            ti = _stop_indexes(dest, stops_n)
            fi = _from_indexes(entry.get("from"), stops_n)
            if ti and fi and ti[-1] <= fi[0] and dest not in leg_from:
                continue
            if not ti and len(leg_from) == 1:
                if _run_serves_pair(suki_index, next(iter(leg_from)), dest):
                    continue
            keep.append(c)
        items = keep
        exact = [
            c for c in items
            if _same_station(
                (suki_index._stops_norm.get(str(c.get("key"))) or [None])[-1], dest
            )
        ]
        if exact:
            items = exact
    if not direct and stt:
        passing = [c for c in items if "dep_run" in c]
        if passing:
            items = passing
    dep_min = _hhmm_min((entry.get("scheduled") or {}).get("dep"))
    leg_origin = suki.norm_station(
        (entry.get("scheduled") or {}).get("origin") or ""
    ) or None

    def rank(c):
        # 过站/发车证据越近越好(gap 口径由 _verify_passage 与
        # _collapse_by_time 统一给出);dep0 只在同首站时可与车段表定
        # 相比,中途起算的车段比了反而误导。
        gap = c.get("gap")
        gap = 999 if gap is None else gap
        d0 = c.get("dep0")
        stops = suki_index._stops_norm.get(str(c.get("key"))) or []
        same_origin = bool(
            leg_origin and stops and _same_station(stops[0], leg_origin)
        )
        dev = (
            abs(d0 - dep_min)
            if (same_origin and d0 is not None and dep_min is not None)
            else 0
        )
        fz = 0 if c.get("subseq") or c.get("direct") else c.get("fuzzy", 99)
        return (gap, dev, fz, c.get("extra", 0), c.get("size", 0))

    items.sort(key=rank)
    if len(items) > 1 and rank(items[0]) == rank(items[1]):
        items = []
    entry["suki"] = items


def _align_scheduled(entry, suki_index):
    """Make scheduled.{dep,origin} describe the suki trip that was pinned.

    The site (index.html delayApplyData) re-finds the trip by
    dep[0] == scheduled.dep + jas[0] == scheduled.origin. scheduled comes
    from the dokorail leg, whose origin can be a mid-route station (仙台
    始発 228M 报岩沼: leg origin 岩沼, tmap origin 仙台) and whose dep can
    differ from tmap by a few minutes. Writing the leg's values made the
    site drop otherwise-correct pins (~370 rows). dep0 only exists after
    passage/time verification, so this never invents a run."""
    items = entry.get("suki") or []
    if not items:
        return
    dep0 = items[0].get("dep0")
    key = str(items[0].get("key"))
    raw = (suki_index.trains.get(key) or {}).get("s_jas") or []
    if dep0 is None or not raw:
        return
    sched = dict(entry.get("scheduled") or {})
    sched["dep"] = f"{dep0 // 60:02d}:{dep0 % 60:02d}"
    sched["origin"] = raw[0]
    if not sched.get("stops"):
        sched["stops"] = raw
    entry["scheduled"] = sched


_SITE_PAREN = re.compile(r"\([^)]*\)")


def _site_norm(name):
    """Exact mirror of the site's normName: ASCII parens only, ケ/ヶ unify,
    drop 駅. This is the yardstick for "will the site match?", so it must not
    fold anything the site does not fold."""
    if not name:
        return ""
    n = _SITE_PAREN.sub("", str(name))
    n = n.replace("ケ", "ヶ")
    n = re.sub(r"駅$", "", n)
    return n.strip()


def _fold_norm(name):
    """_site_norm plus NFKC: feeds and tmap spell compatibility ideographs
    and full-width digits differently (京王 笹塚 U+FA10 vs 笹塚 U+585A,
    空港第２ビル vs 空港第2ビル(鉄道)). Used only to FIND the tmap spelling -
    the site compares exactly, so _align_from must write that spelling back
    before the payload ships."""
    if not name:
        return ""
    return _site_norm(unicodedata.normalize("NFKC", str(name)))


def _align_from(entry, suki_index):
    """Rewrite `from` into the pinned tmap's own spelling when the feed's
    spelling does not survive the site's exact normName comparison
    (空港第２ビル vs 空港第2ビル(鉄道), 笹塚 vs 笹塚, section text 繁藤～新改).
    The site re-finds the trip by `from` membership, so a spelling-only
    mismatch silently drops an otherwise correct pin."""
    fr = entry.get("from")
    items = entry.get("suki") or []
    if not fr or not items:
        return
    raw = (suki_index.trains.get(str(items[0].get("key"))) or {}).get("s_jas") or []
    if not raw:
        return
    want = _fold_norm(fr)
    hit = next((s for s in raw if _fold_norm(s) == want), None)
    if hit is not None:
        if _site_norm(hit) != _site_norm(fr):
            entry["from"] = hit
        return
    frags = [fr] + [p.strip() for p in re.split(r"[~～]", fr) if p.strip()]
    for frag in frags:
        nf = _fold_norm(frag)
        if not nf:
            continue
        nfs = suki.norm_station(frag)
        for s in raw:
            if _fold_norm(s) == nf or suki.norm_station(s) == nfs:
                entry["from"] = s
                return


def resolve_direct_by_time(stt, suki_index, cands, station, now, delay_min,
                            train_type=None, tol=DIRECT_TIME_TOLERANCE_MIN,
                            official=None, dest=None, max_extra=1, origin=None):
    """`_resolve_direct_pool` with an optional hard origin hint.

    `origin` is only passed by feeds that report the train's own first station
    (西武 startStationName). suki carries many short-turn keys that differ
    only in where they start (豊島園/石神井公園/小手指→池袋), and passage
    minutes alone cannot tell them apart - the same clock time lands on two
    different services. When the pool holds any origin-matching candidate the
    gate is hard: resolving against an origin-mismatched key would pin the
    delay to another train. Only when nothing in the pool starts there (a
    mid-route 運転区間 change, or a key that models a longer through run) does
    the whole pool stay in play."""
    if origin:
        want_origin = suki.norm_station(origin)
        gated = [
            c for c in cands
            if (suki_index._stops_norm.get(str(c["key"])) or [None])[0] == want_origin
        ]
        if gated and len(gated) < len(cands):
            return _resolve_direct_pool(
                stt, suki_index, gated, station, now, delay_min,
                train_type, tol, official, dest, max_extra)
    return _resolve_direct_pool(
        stt, suki_index, cands, station, now, delay_min,
        train_type, tol, official, dest, max_extra)


def _resolve_direct_pool(stt, suki_index, cands, station, now, delay_min,
                         train_type=None, tol=DIRECT_TIME_TOLERANCE_MIN,
                         official=None, dest=None, max_extra=1):
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
    want_dest = suki.norm_station(dest) if dest else None
    scored = []
    for relaxed in (False, True):
        # cloudrail 只发布 planArrival(到站),严格口径比的却是 passage_times
        # (发车);始发/长停站差一个 dwell,整列车会被推出窗口。发车口径全灭
        # 时补一轮 ±1 的『到达或发车』比对。放宽窗口里同一模式若出现两趟,
        # 说明该模式自身有歧义,整轮证据作废(宁漏不误报)。
        if relaxed and (official is None or official.get("departure") is None):
            break
        scored = []
        veto = False
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
            if official is not None and not _stops_consistent(stops_n, want, official, max_extra):
                continue
            anchor = want if want in stops_n else next(
                s for s in stops_n
                if s.startswith(want) and s[len(want):].endswith(("都", "道", "府", "県"))
            )
            if relaxed:
                near = [(min(abs(a - est), abs(d - est)), p)
                        for a, d, p in stt.stop_times(key, stops_n, anchor)
                        if abs(a - est) <= 1 or abs(d - est) <= 1]
                if len(near) > 1:
                    veto = True
                    break
                if near:
                    scored.append((near[0][0], c, key, near[0][1]))
                continue
            pas = stt.passage_times(key, stops_n, anchor)
            near = [p for p in pas if abs(p[0] - est) <= tol] if pas else []
            if len(near) > 1:
                # peak headways (大江戸線 3 min) put two runs of one pattern
                # inside ±3. An exact scheduled-minute hit is still decisive
                # when the next run is >=3 min away; an inexact hit is not.
                near.sort(key=lambda p: abs(p[0] - est))
                if near[0][0] == est and near[1][0] - near[0][0] >= tol:
                    near = near[:1]
            if len(near) != 1:
                continue
            arrive, dep0 = near[0]
            scored.append((abs(arrive - est), c, key, dep0))
        if veto:
            scored = []
        if scored:
            break
    if not scored and official is None:
        # 报站命中多个模式、但「终点+报告站+车种」筛完只剩一个，严格窗口
        # 却没对上：多半是 feed 页面刷新滞后把 now-delay 推后了几分钟。
        # 同一模式前后班次间距远大于滞后误差时，最近那趟仍然唯一。
        surv = []
        for c in cands:
            stops_n = suki_index._stops_norm[str(c["key"])]
            if want_dest and stops_n and stops_n[-1] != want_dest:
                continue
            if want not in stops_n and not suki_index._has_pref_suffixed(stops_n, want):
                continue
            if not _type_compatible(stops_n, train_type):
                continue
            surv.append((c, stops_n))
        if len(surv) == 1:
            c, stops_n = surv[0]
            key = str(c["key"])
            anchor = want if want in stops_n else next(
                s for s in stops_n
                if s.startswith(want) and s[len(want):].endswith(("都", "道", "府", "県"))
            )
            near = sorted(
                (abs(p[0] - est), p) for p in stt.passage_times(key, stops_n, anchor) or []
            )
            if near and near[0][0] <= SINGLETON_STALE_TOLERANCE_MIN and (
                    len(near) == 1
                    or near[1][0] - near[0][0] >= SINGLETON_RUN_SEPARATION_MIN):
                scored.append((near[0][0], c, key, near[0][1][1]))
                tol = SINGLETON_STALE_TOLERANCE_MIN
    if not scored:
        return None
    scored.sort(key=lambda x: x[0])
    # Exact time match (gap=0) is unambiguous - take it even if a 2nd
    # candidate is close. Otherwise require >2 min gap from runner-up.
    if scored[0][0] > 0 and len(scored) >= 2 and scored[1][0] - scored[0][0] <= 2:
        return None
    gap, cand, key, dep0 = scored[0]
    if gap > tol:
        return None
    stops = suki_index.trains[key]["s_jas"]
    c2 = dict(cand)
    c2["size"] = len(stops)
    c2["dep0"] = dep0
    c2["gap"] = gap
    scheduled = {
        "dep": f"{dep0 // 60:02d}:{dep0 % 60:02d}",
        "origin": stops[0],
        "stops": stops,
    }
    return {"cand": c2, "scheduled": scheduled}


def _resolve_by_trip_dep0(stt, suki_index, cands, dep0, dest, now, delay_min,
                          cur_station=None):
    """用 feed 自报班次号里的表定始发分钟直接钉车(函館市電 ICAS 第19列)。

    这条路比按报站时刻猜强得多：班次号是车辆自己声明的身份，只要
    「同一发车分钟 + 同一终点站」在当日时刻表里唯一，就是这趟车。
    时刻表自身也保证唯一：函館市電 同终点的 key 之间没有任何共享 dep0。

    再用车辆当前站(d[16])本轮的表定通过时刻 vs now-delay 做闸门——
    班次号过期、时刻表漂移或车辆不在自己线路上时宁可不解(宁漏不误报)。
    """
    if not stt or dep0 is None:
        return None
    want_dest = suki.norm_station(dest) if dest else None
    if not want_dest:
        return None
    hits = []
    for c in cands:
        key = str(c["key"])
        stops_n = suki_index._stops_norm.get(key) or []
        if not stops_n or stops_n[-1] != want_dest:
            continue
        if dep0 in (stt.runs.get(key) or []):
            hits.append((key, c, stops_n))
    if len(hits) != 1:
        return None
    key, cand, stops_n = hits[0]
    cur = suki.norm_station(cur_station) if cur_station else None
    if cur:
        if cur not in stops_n:
            return None
        est = now.hour * 60 + now.minute - (delay_min or 0)
        this_run = [p for p in (stt.passage_times(key, stops_n, cur) or [])
                    if p[1] == dep0]
        if not this_run:
            return None
        if min(abs(a - est) for a, _ in this_run) > TRIP_ID_TIME_TOLERANCE_MIN:
            return None
    c2 = dict(cand)
    c2["size"] = len(stops_n)
    c2["dep0"] = dep0
    c2["gap"] = 0
    stops = suki_index.trains[key]["s_jas"]
    return {"cand": c2, "scheduled": {
        "dep": f"{dep0 // 60:02d}:{dep0 % 60:02d}",
        "origin": stops[0],
        "stops": stops,
    }}


def _resolve_meitetsu_relaxed(stt, suki_index, cands, station, now,
                               delay_min, train_type, official, dest):
    """Meitetsu-only fallback (called only after the strict time resolver
    returns None). Nagoya Main Line packs 特急/急行/準急/普通 into 3-minute
    windows, so the strict ±3-min unique-run test drops them. Instead we hard
    filter on the three discriminating signals — current station + train type
    + terminal — which selects the unique stop-pattern, then pick that pattern's
    temporally closest run. Bounded to Meitetsu: never touches other operators.
    Safely returns None when the pattern isn't unique (宁漏不误报)."""
    if not stt or not station:
        return None
    want = suki.norm_station(station)
    want_dest = suki.norm_station(dest) if dest else None
    est = now.hour * 60 + now.minute - (delay_min or 0)
    if official and official.get("departure") is not None:
        est = official["departure"]
    pattern_keys = []
    loose_keys = []
    for c in cands:
        key = str(c["key"])
        stops_n = suki_index._stops_norm.get(key)
        if not stops_n or want not in stops_n:
            continue
        if want_dest and stops_n[-1] != want_dest:
            continue
        if not _type_compatible(stops_n, train_type):
            continue
        loose_keys.append((c, key, stops_n))
        # official 的后续停站是「停车」序列，suki 的 key 是线路通过站
        # 全序列（急行会多出被通过的站），前缀比对必然失败。这里只是
        # 优选条件：站序对得上时优先；对不上时仍允许在「终点+种别+
        # 报告站」唯一确定模式后按官方表定时刻挑运行（上面 ≤3 分钟的
        # 时间闸门保证不会挑到邻班）。
        if official and not _stops_consistent(stops_n, want, official):
            continue
        pattern_keys.append((c, key, stops_n))
    if official and not pattern_keys:
        pattern_keys = loose_keys
        strict = False
    else:
        strict = True
    # require a unique stop-pattern, else stay ambiguous
    uniq = {k for _, k, _ in pattern_keys}
    if len(uniq) != 1:
        return None
    c, key, stops_n = pattern_keys[0]
    pas = stt.passage_times(key, stops_n, want)
    if not pas:
        return None
    # closest run within a generous 20-min window; require it to be a clear win
    best = min(pas, key=lambda p: abs(p[0] - est))
    # 站序一致时沿用宽窗(20 分钟); 只靠「终点+种别+报告站」唯一确定
    # 模式时必须贴着官方表定时刻, 否则就是按最近班次猜。
    if abs(best[0] - est) > (20 if strict else 3):
        return None
    dep0 = best[1]
    stops = suki_index.trains[key]["s_jas"]
    c2 = dict(c)
    c2["size"] = len(stops)
    return {"cand": c2, "scheduled": {
        "dep": f"{dep0 // 60:02d}:{dep0 % 60:02d}",
        "origin": stops[0], "stops": stops}}


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
    leg_origin = None
    if leg_stops:
        ln = [suki.norm_station(s) for s in leg_stops]
        leg_origin = ln[0]
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
        # dep0 只在 key 首站 == 车段首站时同基准:中途起算的车段
        # (仙台始発 228M 报岩沼)dep0 天然比车段表定早一段车程,拿它
        # 比 dep_feed 会把真 key 排到最后;这种候选只能靠过站 gap 决胜。
        origin_match = bool(
            leg_origin and stops_n and _same_station(stops_n[0], leg_origin)
        )
        for p, d in pas:
            dep_dev = (
                abs(d - dep_feed)
                if (dep_feed is not None and origin_match)
                else 0
            )
            scored.append((dep_dev, abs(p - est), c, p, d, origin_match))
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
    dep_dev, gap, cand, pmin, dep0, origin_match = scored[0]
    # 时间证据是硬门槛:主站只读 suki[0] 并按 scheduled.dep 在 tmap 里找班次,
    # 一个既不在报告站过站时刻吻合(gap<=tol)、又不在始发按表定分钟发车
    # (同首站时 dep_dev<=3)的候选就是"停站相似的另外一列车"。放行会把它
    # 钉到本站时刻表的同名班次上(204M 类型错配)。无证据→退回原候选,
    # by_key 因缺 dep_run 拒绝写入(宁漏不误报)。
    if gap > tol and not (
        origin_match and dep_feed is not None and dep_dev <= 3
    ):
        return cands
    c2 = dict(cand)
    c2["dep_run"] = f"{pmin // 60:02d}:{pmin % 60:02d}"
    # suki 口径的发车分钟(与主站 train.depMin 同基准):by_key 的 dep 决胜
    # 用它,而不是 dokorail 的 scheduled.dep(字符串,前端 JS 相减变 NaN)
    c2["dep0"] = dep0
    c2["gap"] = gap
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
                try:
                    blocks = common.bundle_blocks(
                        self.source.bundle(asset["url"]) or []
                    )
                except Exception as exc:  # noqa: BLE001 - degrade to no legs
                    # one flaky bundle (tiles.dokorail.jp stops answering
                    # mid-run) must not abort the whole bake
                    print(f"bundle {railway}/{cal}: {type(exc).__name__}: {str(exc)[:100]}")
                    blocks = []
                legs = self._legs_from_blocks(railway, blocks)
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
        ("Hankyu", source.hankyu_feed),
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
        ("Sotetsu", source.sotetsu_feed),
        ("Odakyu", source.odakyu_feed),
        # Osaka Metro line-level operation status (official operationinfo API,
        # keyless; position/congestion stays in osakametro-congestion.json)
        ("OsakaMetro", source.osakametro_feed),
        ("Keio", source.keio_feed),
        # Meitetsu train-running-position HTML pages (keyless, server-rendered)
        ("Meitetsu", source.meitetsu_feed),
        # Tokyo Metro keyless line-level status (no ODPT key needed; with
        # ODPT_KEY the odpt feed below already carries per-train real data)
        ("TokyoMetro", source.tokyo_metro_feed),
        # Tokyo Metro per-train positions + delays from the App's anonymous
        # Cloud Firestore (tokyometro/{lineId}); Toei 4 lines stay on odpt.py
        ("TokyoMetro", source.tokyo_metro_trains_feed),
        # --- jrstatus batch: Wave1A/B/C + Wave3 Tokyo Glance (34 operators) ---
        ("Aonami", source.aonami_feed),
        ("Aikan", source.aikan_feed),
        ("Hokuhoku", source.hokuhoku_feed),
        ("Ainokaze", source.ainokaze_feed),
        ("HapiLine", source.hapiline_feed),
        ("IRIshikawa", source.ishikawa_feed),
        ("IGR", source.igr_feed),
        ("Yamagata", source.yamagata_feed),
        ("FukuokaSubway", source.fukuoka_subway_feed),
        ("KumamotoTram", source.kumamoto_tram_feed),
        ("Nagasaki", source.nagasaki_feed),
        ("Hiroden", source.hiroden_feed),
        ("OkayamaTram", source.okayama_tram_feed),
        ("Mizushima", source.mizushima_feed),
        ("Manyo", source.manyo_feed),
        ("NagoyaMunicipal", source.nagoya_feed),
        ("SendaiMunicipal", source.sendai_feed),
        ("Tenhama", source.tenhama_feed),
        ("Toyama", source.toyama_feed),
        # Tosaden intentionally absent: tosaden-rb.kochi-mobility.net's
        # IchigoMap.txt is とさでん's BUS map (モビナビ高知 - the client JS
        # only knows バス/バス停), so its rows tagged bus trips onto tram
        # keys and flooded the board (502 false delays on 2026-10-29).
        ("Enoden", source.enoden_feed),
        ("Mizuma", source.mizuma_feed),
        ("HakodateTram", source.hakodate_tram_feed),
        ("Toyohashi", source.toyohashi_feed),
        ("TG-Kanto", source.tg_kanto_feed),
        ("TG-Komatsu", source.tg_komatsu_feed),
        ("TG-Nagareyama", source.tg_nagareyama_feed),
        ("TG-Yamaman", source.tg_yamaman_feed),
        ("TG-Joshin", source.tg_joshin_feed),
        ("TG-Jomo", source.tg_jomo_feed),
        ("TG-Moka", source.tg_moka_feed),
        ("TG-Watarase", source.tg_watarase_feed),
        ("TG-Fujikyu", source.tg_fujikyu_feed),
        ("TG-Sunzu", source.tg_sunzu_feed),
        ("TG-Daiyuzan", source.tg_daiyuzan_feed),
        ("TG-Izukyu", source.tg_izukyu_feed),
        ("TG-HakoneTozan", source.tg_hakonetozan_feed),
        ("TG-Rinkai", source.tg_rinkai_feed),
        ("TG-Chichibu", source.tg_chichibu_feed),
        ("TG-KashimaRinkai", source.tg_kashimarinkai_feed),
        ("TG-Hitachinaka", source.tg_hitachinaka_feed),
        ("TG-Isumi", source.tg_isumi_feed),
        ("TG-Choshi", source.tg_choshi_feed),
        ("TG-ShonanMonorail", source.tg_shonanmonorail_feed),
        ("TG-Minatomirai", source.tg_minatomirai_feed),
        ("TG-Hokuso", source.tg_hokuso_feed),
        ("TG-ToyoRapid", source.tg_toyorapid_feed),
        ("TG-ChibaMonorail", source.tg_chibamonorail_feed),
        ("TG-TokyoMonorail", source.tg_tokyomonorail_feed),
        ("TG-Yurikamome", source.tg_yurikamome_feed),
        ("TG-TamaMonorail", source.tg_tamamonorail_feed),
        ("TG-SaitamaRailway", source.tg_saitamarailway_feed),
        ("TG-SaitamaTransit", source.tg_saitamatransit_feed),
        ("TG-UtsunomiyaLRT", source.tg_utsunomiyalrt_feed),
        ("TG-YokohamaSeaside", source.tg_yokohamaseaside_feed),
        ("TG-YokohamaMunicipal", source.tg_yokohamamunicipal_feed),
        ("TG-Shibayama", source.tg_shibayama_feed),
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
                entry["to"] = _feed_dest(op, t)
            elif not entry.get("to"):
                # ODPT lines carry the service terminal as a station id
                # (Toei/Tobu). Through-runs to another operator's line name a
                # terminal that no suki pattern on this line ends at, so this
                # only narrows - resolve ignores it when nothing matches.
                dests = t.get("odpt:destinationStation")
                if isinstance(dests, str):
                    dests = [dests]
                for d in dests or []:
                    name = line.get("stations_by_id", {}).get(
                        (d or "").rsplit(".", 1)[-1]
                    )
                    if name:
                        entry["to"] = name
                        # resolve's terminal gate is hard; only a real station
                        # name may reach it (Tobu/Tokyu feeds also put
                        # "◯◯線直通" in `to`, which no pattern terminus equals)
                        t["_odpt_dest"] = name
                        break
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
                if not leg and t.get("_station") and not t.get("_skip_position_leg"):
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
                # ODPT feeds (Toei/Tobu/YokohamaMunicipal) carry only
                # odpt:fromStation, resolved into entry["from"]; they never
                # set _station/_line_ja, so without these fallbacks the whole
                # suki-direct path never runs for them.
                report_station = t.get("_station") or entry.get("from")
                suki_pool = (
                    t.get("_suki_line") or t.get("_line_ja") or entry.get("line")
                )
                if not leg and (t.get("_station_keys") or (report_station and suki_pool)):
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
                                  else (t.get("_suki_pools")
                                        or [suki_pool]))
                    direct = None
                    for station in (t.get("_station_keys") or [report_station]):
                        if not station:
                            continue
                        direct = _direct_candidates(
                            suki_index, lines_pool, station, direction,
                            entry.get("type"), railway)
                        if direct:
                            break
                        # feed 的线路标签与实跑线路不一致（宮崎空港線直通车在
                        # 日豊本線浅海井上报站、予讃線コードが土讃線区間を
                        # 報告）。仅当该站名在 suki 里只属于唯一一条线路池时
                        # 才用该池重试——多池站点可能是跨运营方同名站
                        # （新宿/高松），不做兜底，维持宁漏不误报。
                        pools = suki_index.pools_for_station(station)
                        if len(pools) == 1:
                            direct = _direct_candidates(
                                suki_index, pools, station, direction,
                                entry.get("type"), railway)
                            if direct:
                                break
                    if direct:
                            # the fragment that produced the candidates is the
                            # real reported station ("繁藤～新改" sections);
                            # resolving against the composite text matches no
                            # stop at all and always returns None
                            match_station = station
                            entry["suki"] = direct
                            entry["match"] = "suki-direct"
                            # 時刻消歧：无 dokorail 时刻表的线路（新幹線等）
                            # 用 suki 時刻表把多候選收斂為唯一班次並補
                            # scheduled（主站打標需要 dep/origin/stops）；
                            # JR-Central 官方発車順序可用時，以官方表定時刻
                            # 為基準 + 官方停站序列做站序一致性校驗；
                            # 消歧不出 → 保持多候選，by_key 只信單候選 → 不映射
                            official = None
                            # 单候选也必须解一次: 主站需要 scheduled.dep 才能
                            # 按分钟回查班次,而 suki-direct 路径的 dep 只能由
                            # 表定时刻给出(len(direct)==1 时也不例外)。
                            # feed 自报班次号(函館市電)时先走精确钉车。
                            resolved = None
                            if stt and t.get("_trip_dep0") is not None:
                                resolved = _resolve_by_trip_dep0(
                                    stt, suki_index, direct, t["_trip_dep0"],
                                    _feed_dest(op, t) or t.get("_odpt_dest"),
                                    now, round(delay / 60),
                                    cur_station=t.get("_cur_station"),
                                )
                            widened = False
                            if not resolved and stt:
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
                                    match_station, now, round(delay / 60),
                                    entry.get("type"), official=official,
                                    dest=_feed_dest(op, t) or t.get("_odpt_dest"),
                                    # cloudrail 只发布报站后的 1-2 站（1253M
                                    # 羽鳥→岩間・友部，实际还到勝田 4 站），
                                    # max_extra=1 会把整列车判成站序不一致；
                                    # 前缀+终点站+时刻唯一性已能锁定同一列车
                                    # (comfort 对同一 feed 也传 None)
                                    max_extra=None if t.get("_official") else 1,
                                    origin=t.get("_start"),
                                )
                                if not resolved and op == "Meitetsu":
                                    # Bounded fallback: current station + type +
                                    # terminal selects the unique stop-pattern,
                                    # then closest run. Only Meitetsu.
                                    resolved = _resolve_meitetsu_relaxed(
                                        stt, suki_index, direct,
                                        match_station, now,
                                        round(delay / 60),
                                        entry.get("type"), official,
                                        dest=_feed_dest(op, t))
                                widened = False
                                if (not resolved and official is not None
                                        and official.get("departure") is not None
                                        and official.get("stations")):
                                    # 线路标签只覆盖列车的一段 (川越線)，同一趟车
                                    # 在 suki 里带着多段线路名 (東北本線/赤羽線/
                                    # りんかい線)，按标签筛的池子会漏掉它。官方
                                    # 停站序列是更强的身份证据：整池重扫一次，
                                    # 只有站序前缀+终点站都对得上的 key 能进来，
                                    # 且仅在按标签的池子已经解不出时才尝试。
                                    wide = _widen_official_candidates(
                                        suki_index, direct, match_station, official)
                                    if len(wide) > len(direct):
                                        resolved = resolve_direct_by_time(
                                            stt, suki_index, wide,
                                            match_station, now, round(delay / 60),
                                            entry.get("type"), official=official,
                                            dest=_feed_dest(op, t) or t.get("_odpt_dest"),
                                            max_extra=None,
                                            origin=t.get("_start"),
                                        )
                                        widened = resolved is not None
                                if resolved:
                                    # identity is the scheduled run, not the
                                    # stop pattern: a key holds many runs and
                                    # two live trains may legitimately sit on
                                    # different runs of the same pattern
                                    rk = (str(resolved["cand"]["key"]),
                                          resolved["cand"].get("dep0"))
                                    if rk in claimed_suki:
                                        # 只有「标签池就解出了」才是重复行（同车
                                        # 多次上报），整行丢弃；放宽池撞上已被认领
                                        # 的班次说明这是另一趟车，保留未钉状态，
                                        # 不能因为它消失
                                        if widened:
                                            resolved = None
                                        else:
                                            continue
                                    else:
                                        claimed_suki[rk] = t.get("number", "?")
                                        entry["suki"] = [resolved["cand"]]
                                        entry["scheduled"] = resolved["scheduled"]
                                        if match_station != report_station:
                                            entry["from"] = match_station
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
                cands = _match_candidates(
                    suki_index, stations, suki_line, railway, MATCH_TOP
                )
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
                    cands = _match_candidates(
                        suki_index, stations, suki_line, railway, 256
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
        # 站点按 (key, 发车分钟) 落点,同一班车被多家 feed 各报一条时
        # (JR-East 的 dokotrain + cloudrail app)两条会钉到同一个班次,
        # 只让「延误列车」计数虚高。等对齐完成后再按班次去重。
        # 站点只读 suki[0]，必须先把反向/错终点/未验证的候选压到后面或
        # 丢弃；时间并列且证据完全相同的两个 key 无法确定真身→宁漏
        _finalize_candidates(e, stt, suki_index)
        if not (e.get("suki") or []):
            _pin_by_endpoints(e, suki_index, stt)
        # scheduled.dep/origin 改成所钉 tmap 班次自己的口径，站点才能
        # 按「精确发车分钟 + 始发站」回查到这一班（否则中途起算的车段
        # 会被站点整条丢弃）
        _align_scheduled(e, suki_index)
        # from 也要能通过站点的精确比对（全角数字/区间文本会被整条丢弃）
        _align_from(e, suki_index)
        cand_items = e.get("suki") or []
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

    def _trip_id(e):
        """(tmap key, run departure minute) — the identity the site pins on."""
        items = e.get("suki") or []
        if len(items) != 1 or items[0].get("key") is None:
            return None
        dep = items[0].get("dep0")
        if dep is None:
            dep = _hhmm_min((e.get("scheduled") or {}).get("dep"))
        return None if dep is None else (str(items[0]["key"]), dep)

    def _pin_rank(e):
        s = e.get("scheduled") or {}
        return (
            1 if s.get("dep") and s.get("origin") and e.get("from") else 0,
            0 if e.get("match") == "suki-direct" else 1,
            1 if "dep_run" in ((e.get("suki") or [{}])[0]) else 0,
        )

    kept_trips = {}
    deduped = []
    for e in board:
        tid = _trip_id(e)
        if tid is None:
            deduped.append(e)
            continue
        hit = kept_trips.get(tid)
        if hit is None:
            kept_trips[tid] = (len(deduped), e)
            deduped.append(e)
        elif _pin_rank(e) > _pin_rank(hit[1]):
            # 同一班车,保留证据更硬的一条(站点能钉住 → 站序验证 → 非 direct)
            deduped[hit[0]] = e
            kept_trips[tid] = (hit[0], e)
    board = deduped
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
