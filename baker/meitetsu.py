"""Meitetsu (名古屋鉄道) live train-delay feed via the public train-running-position
web pages.

Reverse-engineered 2026-10-27. The site renders every live train as a
server-side HTML popup on one page per line-group; there is no JSON API.

    GET https://top.meitetsu.co.jp/trainlocation/TrainRunningPosition{CODE}.aspx

Each Japanese `.PopupTrainInformation` popup contains, per train:
    種別 (type: 特急/快速特急/急行/準急/普通/ミュースカイ)
    行先 (destination, e.g. 豊橋 行)
    N分遅れ (delay minutes — present only when delayed)
    停車駅 table: 駅名 | 定時着時刻 | 定時発時刻 (full scheduled route)
    備考 <線名> (line name, e.g. 名古屋本線)

No train number is exposed. Matching to suki therefore relies on the
scheduled stops table (station + 定時発時刻) exactly like the JR-Central
time-anchored path. We emit only delayed trains (delay >= 1 min).
"""

import re
import urllib.request

from . import common

BASE = "https://top.meitetsu.co.jp/trainlocation/TrainRunningPosition{}.aspx"
# Line-group page codes enumerated from the index (SelectStationOrLine.aspx).
# Do not trim to a hardcoded subset: the site adds pages per branch line.
PAGES = ["NH", "TK", "GN1", "GN2", "MU", "MY", "TT", "TA", "CH",
         "KC1", "KC2", "TB", "BS1", "BS2", "TH", "IY",
         "HM", "KG", "KM", "ST"]

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

DELAY_THRESHOLD_MIN = 1  # emit delay >= 60 s


def _get(url, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Referer": "https://top.meitetsu.co.jp/trainlocation/",
            })
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as exc:  # network flakiness: retry then give up
            last = exc
            if attempt < retries:
                import time
                time.sleep(1.5)
    raise last


def _text(node):
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _parse_page(html):
    """Return list of dicts: {line, type, dest, delay, stops:[{name,arr,dep}]}."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    out = []
    # Japanese popups start with 列車情報; English twins start with Train Info.
    for pop in soup.select(".PopupTrainInformation"):
        head = pop.get_text(" ", strip=True)[:12]
        if "列車情報" not in head:
            continue  # skip English twin
        t = _text(pop)

        m_line = re.search(r"備考\s*([一-龥]+線)", t)
        line = m_line.group(1) if m_line else None

        # Flattened text order is: "種別 行先 <type> (<...特別車>) <dest> 行
        # [特別車のご利用は | N分遅れ | 停車駅...]. So split after the 行先
        # label and parse the value segment up to 停車駅.
        seg = t.split("行先", 1)[1] if "行先" in t else t
        seg = seg.split("停車駅", 1)[0]
        # Type = leading known Meitetsu train type keyword.
        train_type = next((ty for ty in
                           ["ミュースカイ", "ﾐｭｰｽｶｲ", "快速特急", "特急",
                            "急行", "準急", "普通"]
                           if seg.lstrip().startswith(ty)), None)
        if train_type:  # drop the type word, else "急行" matches the 行 regex
            seg = seg.split(train_type, 1)[1]
        # Destination = the "<name> 行" token in the remaining segment.
        md = re.search(r"([一-龥ケガ・]+?)\s*行", seg)
        dest = md.group(1).strip() if md else None

        m_delay = re.search(r"(\d+)\s*分遅れ", t)
        delay = int(m_delay.group(1)) if m_delay else 0

        # Stops table (table.TableTI): header row + rows of (空, 駅, 着, 発).
        stops = []
        tbl = pop.find("table", class_="TableTI")
        if tbl is not None:
            for row in tbl.find_all("tr"):
                cells = [c.get_text(" ", strip=True)
                         for c in row.find_all(["th", "td"])]
                cells = [c for c in cells if c]
                if len(cells) < 2 or cells[0] == "停車駅":
                    continue
                # cells: [駅名, 着, 発] 或 [駅名, 時刻]
                name = cells[0]
                times = [c for c in cells[1:] if re.match(r"^\d{2}:\d{2}$", c)]
                if not name or not times:
                    continue
                stops.append({"name": name, "arr": times[0],
                              "dep": times[1] if len(times) > 1 else times[0]})
        out.append({"line": line, "type": train_type, "dest": dest,
                    "delay": delay, "stops": stops})
    return out


def _direction(dest):
    """上り = 名古屋都心方面; 下り = 郊外方面."""
    if not dest:
        return None
    if any(k in dest for k in ["名古屋", "金山", "栄生", "須ケ口"]):
        return "上り"
    return "下り"


def fetch(src=None):
    src = src or common.Source()
    body = []
    seen = set()
    for code in PAGES:
        try:
            if src.offline:
                html = src.offline.root.joinpath(f"mt-{code}.html").read_text(
                    encoding="utf-8")
            else:
                html = _get(BASE.format(code))
        except Exception as exc:
            print(f"meitetsu {code}: {type(exc).__name__}: {str(exc)[:100]}")
            continue
        try:
            trains = _parse_page(html)
        except Exception as exc:
            print(f"meitetsu parse {code}: {type(exc).__name__}: {str(exc)[:100]}")
            continue
        for tr in trains:
            delay = tr["delay"]
            if delay < DELAY_THRESHOLD_MIN:
                continue
            stops = tr["stops"]
            station = stops[0]["name"] if stops else None
            line = tr["line"] or "名古屋本線"
            # Dedup: the same physical train can appear on two segment pages
            # (e.g. NH + TB both render 名古屋本線 through-services).
            first_dep = stops[0]["dep"] if stops else None
            key = (line, station, tr["dest"], tr["type"], first_dep)
            if key in seen:
                continue
            seen.add(key)
            # Official scheduled timetable -> time-anchored suki disambiguation
            # (resolve_direct_by_time expects _official = {departure, stations,
            # terminal} and _sched.stops; without these the multi-candidate
            # suki-direct pool stays ambiguous and is dropped 宁漏不误报).
            off = None
            if stops:
                hh, mm = stops[0]["dep"].split(":")
                off = {"departure": int(hh) * 60 + int(mm),
                       "stations": [s["name"] for s in stops],
                       "terminal": tr["dest"]}
            body.append({
                "owl:sameAs": f"Meitetsu:{line}:{station}:{tr['dest']}",
                "odpt:railway": f"odpt.Railway:Meitetsu.{line}",
                "odpt:trainNumber": None,
                "odpt:delay": delay * 60,
                "odpt:trainType": tr["type"],
                "_line_ja": line,
                "_suki_line": line,
                "_station": station,
                "_dest": tr["dest"],
                "_direction": _direction(tr["dest"]),
                # No _sched: providing it routes into the self-contained branch
                # and skips suki matching. The feed's own timetable rides in
                # _official, which the suki-direct matcher consumes to anchor.
                "_official": off,
            })
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "top.meitetsu.co.jp/trainlocation",
    }
