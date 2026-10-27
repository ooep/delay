import sys, json
sys.path.insert(0, ".")
from baker import delays, suki, suki_timetable, common, odakyu, tx

data_dir = "data"
idx = suki.SukiIndex.load(data_dir)
stt = suki_timetable.SukiTimetable.load(data_dir, 1)  # 周二
now = common.jst_now()
print("suki_index keys:", len(idx.trains), "| stt keys:", len(stt.runs))


def match_one(t):
    num = str(t.get("odpt:trainNumber") or "")
    direction = None
    if num.isdigit():
        direction = "down" if int(num) % 2 else "up"
    lines_pool = t.get("_suki_pools") or [t.get("_suki_line") or t.get("_line_ja")]
    station = t.get("_station")
    if not station:
        return None, "no-station"
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
        if c["key"] not in seen:
            seen.add(c["key"])
            direct.append(c)
    if not direct:
        return None, "no-cand(lines=%s)" % lines_pool
    if len(direct) == 1:
        return direct[0], "single"
    resolved = delays.resolve_direct_by_time(
        stt, idx, direct, station, now,
        round(t["odpt:delay"] / 60),
        (t.get("odpt:trainType") or "").rsplit(".", 1)[-1] or None,
        dest=t.get("_dest"),
    )
    if resolved:
        return resolved["cand"], "resolved"
    return direct, "multi-%d" % len(direct)


print("=== Odakyu (修复后 lines_pool=_suki_line) ===")
ok = tot = 0
for t in odakyu.fetch()["body"]:
    tot += 1
    cand, why = match_one(t)
    if cand:
        ok += 1
    key = cand.get("key") if isinstance(cand, dict) else "-"
    print("  %s %s->%s dir=%s delay=%ss | %s | key=%s" % (
        t["odpt:trainNumber"], t["_station"], t["_dest"],
        t["_direction"], t["odpt:delay"], why,
        key))
print("Odakyu 命中 %d/%d" % (ok, tot))

print("=== TX (实时) ===")
ok = tot = 0
for t in tx.fetch()["body"]:
    tot += 1
    cand, why = match_one(t)
    if cand:
        ok += 1
    print("  %s %s->%s delay=%ss | %s | key=%s" % (
        t["odpt:trainNumber"], t["_station"], t["_dest"],
        t["odpt:delay"], why, cand.get("key") if cand else "-"))
print("TX 命中 %d/%d" % (ok, tot))
