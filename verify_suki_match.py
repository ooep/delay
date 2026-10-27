"""verify_suki_match.py — suki-direct 匹配正确性回归工具。

对每个逐车 feed 的输出，重放 delays.py 的 suki-direct 匹配并逐条打印
命中情况（single/resolved/multi-N/no-cand）。单条失败不中断整个校验，
feed 不可用（离线/网络）时明确报 skip 而不是抛异常。

用法：
  python verify_suki_match.py                 # 在线（Odakyu/TX/TokyoMetro）
  python verify_suki_match.py --offline DIR   # 用 fixture 目录离线跑 TokyoMetro
"""

import argparse
import json
import sys

sys.path.insert(0, ".")
from baker import delays, suki, suki_timetable, common  # noqa: E402


def match_one(t, stt, idx, now):
    """Replay delays.py's suki-direct matching for one feed record.

    Returns (cand, why): cand is a single dict on success, a list of
    candidates when ambiguous (multi-N), or None. Never raises on a malformed
    record - malformed input returns (None, 'bad-record') instead.
    """
    try:
        num = str(t.get("odpt:trainNumber") or "")
        direction = None
        if num.isdigit():
            direction = "down" if int(num) % 2 else "up"
        lines_pool = t.get("_suki_pools") or [
            t.get("_suki_line") or t.get("_line_ja")
        ]
        station = t.get("_station")
        if not station:
            return None, "no-station"
        delay_raw = t.get("odpt:delay")
        if not isinstance(delay_raw, (int, float)):
            return None, "bad-delay"
        merged = []
        for ln in lines_pool:
            merged += (
                idx.match_direct(
                    ln, station, direction,
                    (t.get("odpt:trainType") or "").rsplit(".", 1)[-1] or None,
                    top=256,
                ) or []
            )
        seen = set()
        direct = []
        for c in merged:
            if not isinstance(c, dict):
                continue
            if c.get("key") not in seen:
                seen.add(c["key"])
                direct.append(c)
        if not direct:
            return None, "no-cand(lines=%s)" % lines_pool
        if len(direct) == 1:
            return direct[0], "single"
        resolved = delays.resolve_direct_by_time(
            stt, idx, direct, station, now,
            round(delay_raw / 60),
            (t.get("odpt:trainType") or "").rsplit(".", 1)[-1] or None,
            dest=t.get("_dest"),
        )
        if resolved:
            return resolved["cand"], "resolved"
        return direct, "multi-%d" % len(direct)
    except Exception as exc:  # noqa: BLE001 - one bad record must not kill the run
        return None, "err:%s" % type(exc).__name__


def key_of(cand):
    """Candidate may be a dict (single) or a list (multi) - both safe."""
    if isinstance(cand, dict):
        return cand.get("key")
    if isinstance(cand, list) and cand:
        return "-multi"
    return None


def check_feed(name, records, stt, idx, now):
    ok = tot = 0
    for t in records or []:
        if not isinstance(t, dict):
            print("  [skip] %s: non-dict record %r" % (name, t))
            continue
        tot += 1
        cand, why = match_one(t, stt, idx, now)
        if cand:
            ok += 1
        print("  %s %s->%s dir=%s delay=%ss | %s | key=%s" % (
            t.get("odpt:trainNumber", "?"),
            t.get("_station", "?"),
            t.get("_dest", "?"),
            t.get("_direction", "?"),
            t.get("odpt:delay", "?"),
            why,
            key_of(cand)))
    print("%s 命中 %d/%d" % (name, ok, tot))
    return ok, tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", default=None, help="fixture dir for TokyoMetro")
    args = ap.parse_args()

    data_dir = "data"
    idx = suki.SukiIndex.load(data_dir)
    stt = suki_timetable.SukiTimetable.load(data_dir, 1)  # 周二
    now = common.jst_now()
    print("suki_index keys:", len(idx.trains), "| stt keys:", len(stt.runs or {}))

    # Odakyu (在线)
    print("=== Odakyu (在线) ===")
    try:
        odakyu = __import__("baker.odakyu", fromlist=["fetch"])
        rec = odakyu.fetch() or {}
        check_feed("Odakyu", rec.get("body"), stt, idx, now)
    except Exception as exc:
        print("Odakyu feed unavailable: %s: %s" % (type(exc).__name__, str(exc)[:120]))

    # TX (在线)
    print("=== TX (在线) ===")
    try:
        tx = __import__("baker.tx", fromlist=["fetch"])
        rec = tx.fetch() or {}
        check_feed("TX", rec.get("body"), stt, idx, now)
    except Exception as exc:
        print("TX feed unavailable: %s: %s" % (type(exc).__name__, str(exc)[:120]))

    # TokyoMetro 逐车（在线；--offline 时用 fixture）
    print("=== TokyoMetro 逐车 (%s) ===" % ("离线 fixture" if args.offline else "在线"))
    try:
        from baker import tokyometro_firestore
        src = common.Source(args.offline)
        rec = tokyometro_firestore.fetch(src) or {}
        check_feed("TokyoMetro", rec.get("body"), stt, idx, now)
    except Exception as exc:
        print("TokyoMetro feed unavailable: %s: %s" % (type(exc).__name__, str(exc)[:120]))


if __name__ == "__main__":
    main()
