"""JR-West (JR西日本) live train feed via the official train-guide API.

Source: https://www.train-guide.westjr.co.jp/api/v3/ - the company's own
position service (plain JSON, no key; CloudFront in front):
    area_<area>_master.json   line roster per area (kinki, hokuriku,
                              okayama, hiroshima, sanin)
    <slug>.json               trains in transit per line
    <slug>_st.json            station code -> name per line

Train record: {no: "1303", pos: "2511_2512" ("2510_####" when stopped at
2510), direction, displayType: "普通", delayMinutes: <int minutes>,
numberOfCars, dest: {...}}. Numbers are bare (1303) while dokorail bundles
carry suffixes/variations (1303M, 1303~1), so the LegStore prefix match
covers them.

Replaces the dokorail JR-West feed (Cloudflare 403s GitHub runner IPs);
delays.py excludes JR-West from its dokorail loop and pulls this instead.
Line names are JR-West marketing names (JR京都線, 大和路線, ...) -
LINE_NAME_REWRITES maps them onto the official 本線 names used by dokorail
and jr.suki.ing; delays.py's operator-scoped title bridge resolves the
timetable bundles from there.
"""

import concurrent.futures
import re
import time

from . import common

BASE = "https://www.train-guide.westjr.co.jp/api/v3"
AREAS = ["kinki", "hokuriku", "okayama", "hiroshima", "sanin"]

DELAY_THRESHOLD_MIN = 1
REQUEST_GAP_SEC = 0.05  # politeness: 45+ files per sweep

LINE_NAME_REWRITES = {
    "JR京都線": "東海道本線",
    "JR神戸線・山陽線": "山陽本線",
    "山陽線": "山陽本線",
    "JR宝塚線・福知山線": "福知山線",
    "JR宝塚線": "福知山線",
    "大和路線": "関西本線",
    "学研都市線": "片町線",
    "北陸線・琵琶湖線": "北陸本線",
    "山陰線": "山陰本線",
    "きのくに線": "紀勢本線",
    "万葉まほろば線": "桜井線",
    "JRゆめ咲線": "桜島線",
    "阪和線・羽衣線": "阪和線",
}


def _get(src, path, offline_name):
    if src.offline:
        try:
            return src.offline_json(offline_name)
        except Exception:
            return None
    try:
        time.sleep(REQUEST_GAP_SEC)
        return common.get_json(
            f"{BASE}/{path}", headers={"Referer": "https://www.train-guide.westjr.co.jp/"}
        )
    except Exception as exc:
        print(f"jrwest {path}: {type(exc).__name__}: {str(exc)[:120]}")
        return None


def fetch(src=None):
    src = src or common.Source()
    now = common.jst_now()

    slug_names = {}
    for area in AREAS:
        master = _get(src, f"area_{area}_master.json", f"wg-area-{area}.json")
        for slug, meta in ((master or {}).get("lines") or {}).items():
            name = meta.get("name_ja") or meta.get("name")
            if name:
                slug_names[slug] = name
    if not slug_names:
        return None

    body = []
    delayed_slugs = set()
    pending = []
    stamps = []
    # 45+ per-line files: fetch them concurrently (serial walk was ~3 min on
    # slow links); CloudFront in front of the official API takes the small
    # per-bake burst without complaint
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for slug, d in ex.map(lambda s: (s, _get(src, f"{s}.json", f"wg-trains-{s}.json")), list(slug_names)):
            if d and d.get("update"):
                stamps.append(str(d["update"]))
            for t in (d or {}).get("trains") or []:
                if not isinstance(t, dict):
                    continue
                delay_min = int(t.get("delayMinutes") or 0)
                if delay_min < DELAY_THRESHOLD_MIN:
                    continue
                number = str(t.get("no") or "").strip()
                if not number:
                    continue
                # a through train crossing a line boundary appears under both
                # slugs - keep one record per train number
                if any(p[1] == number for p in pending):
                    continue
                slug_names.setdefault(slug, slug)
                pending.append((slug, number, delay_min, t))
                delayed_slugs.add(slug)

    # station names only for lines that actually have delayed trains.
    # Codes are scoped to the line file (0391 is 篠原 on one line and 和田山
    # on another; 54 such collisions in one sweep), so key by (slug, code) -
    # a global code map both flips with set iteration order and renames
    # trains to another line's station.
    station_names = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for slug, st in ex.map(lambda s: (s, _get(src, f"{s}_st.json", f"wg-st-{s}.json")), sorted(delayed_slugs)):
            for row in (st or {}).get("stations") or []:
                info = row.get("info") or {}
                if info.get("code"):
                    station_names[(slug, info["code"])] = info.get("name")

    def _dest_text(raw):
        # dest is normally {text, code, line} but the live API sometimes
        # emits a bare string; keep both shapes flowing
        if isinstance(raw, dict):
            return raw.get("text")
        return raw if isinstance(raw, str) else None

    for slug, number, delay_min, t in pending:
        raw_name = slug_names.get(slug, slug)
        line_name = LINE_NAME_REWRITES.get(raw_name, raw_name)
        pos = str(t.get("pos") or "")
        at = pos.split("_")[0] if pos else ""
        body.append(
            {
                "owl:sameAs": f"JR-West-guide:{slug}.{number}",
                "odpt:railway": "odpt.Railway:JR-West",
                "odpt:trainNumber": number,
                "odpt:delay": delay_min * 60,
                "odpt:trainType": t.get("displayType") or None,
                "_number_prefix": number,
                # JR-West guide numbers carry a depot letter (2724H) while
                # dokorail bundles use a different depot's letter (2724M)
                # for the same train - match on the digits only
                "_number_digits": re.sub(r"\D", "", number) or None,
                "_line_ja": line_name,
                "_suki_line": line_name,
                "_station": station_names.get((slug, at)),
                "_dest": _dest_text(t.get("dest")),
            }
        )
    return {
        "ok": True,
        "fetchedAt": now.isoformat(),
        "source_time": max(stamps) if stamps else None,
        "body": body,
        "source": "train-guide.westjr.co.jp",
    }
