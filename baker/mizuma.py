"""Mizuma Railway (水間鉄道) live feed.

The public "動的時刻表" Canva shell (mizuma-pr.my.canva.site/doutekijikoku/)
only links out to the real dynamic service:

    GET https://mikey.co.jp/mizuma_timetable/?sta=<1..10>&dir=<1|2>

  sta: 1=貝塚 2=貝塚市役所前 3=近義の里 4=石才 5=清児 6=名越 7=森
       8=三ツ松 9=三ヶ山口 10=水間観音（和泉大学前）
  dir: 1=下り(貝塚→水間観音) 2=上り

Server-rendered PHP page. Sections:
  div.trips_status_status  line-level operation text
                           ("現在、正常に運行されています" / delay text)
  ul.commingBus            NEXT departures at THIS station (during service);
                           out of service it renders only
                           "本日の営業は終了いたしました"
  div.timetable            full static timetable (平日 / 土日祝)

発車予報型判据 (per baker contract): a train counts as delayed only when an
upcoming departure still listed in commingBus has passed its scheduled
clock time by >= 60 s. BLOCKER for verification: probed 2026-10-28 00:10
JST, service already ended ("本日の営業は終了"), so NO live commingBus row
was captured - the row markup (departure time / countdown text) is inferred
from the timetable <li>HH:MM</li> pattern and cannot be confirmed until
daytime re-capture. Marked データ存疑; parser is defensive (emits nothing
when the row shape is unrecognised).
"""

import re
import urllib.request

from . import common

BASE = "https://mikey.co.jp/mizuma_timetable/"

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

DELAY_THRESHOLD_SEC = 60

STATIONS = {
    1: "貝塚", 2: "貝塚市役所前", 3: "近義の里", 4: "石才", 5: "清児",
    6: "名越", 7: "森", 8: "三ツ松", 9: "三ヶ山口", 10: "水間観音",
}


def _get(url, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA,
                              "Referer": "https://mizuma-pr.my.canva.site/"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as exc:
            last = exc
            if attempt < retries:
                import time
                time.sleep(1.5)
    raise last


def _row_delay_min(html, now_min):
    """Inspect ul.commingBus; return (status_text, delayed_dep_minutes) of the
    first upcoming train whose scheduled departure has already passed."""
    m = re.search(r'<div class="trips_status_status">(.*?)</span>', html,
                  re.S)
    status = re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else None
    m = re.search(r'<ul class="commingBus">(.*?)</ul>', html, re.S)
    if not m:
        return status, []
    upcoming = re.findall(r"(\d{1,2}):(\d{2})", m.group(1))
    delayed = []
    for hh, mm in upcoming:
        dep = int(hh) * 60 + int(mm)
        # An upcoming departure already in the past = running late.
        if now_min - dep >= 1:
            delayed.append(dep)
    return status, delayed


def fetch(src=None):
    src = src or common.Source()
    now = common.jst_now()
    now_min = now.hour * 60 + now.minute
    body = []
    # One station page is enough for the line-level status; the per-station
    # commingBus is checked for 貝塚(sts=1) only in this first version - the
    # row format itself is unconfirmed (see module docstring), so we do not
    # fan out across all 20 pages blindly.
    try:
        if src.offline:
            html = src.offline.root.joinpath("mizuma-sta1.html").read_text(
                encoding="utf-8")
        else:
            html = _get(f"{BASE}?sta=1&dir=1")
    except Exception as exc:
        print(f"mizuma fetch: {type(exc).__name__}: {str(exc)[:120]}")
        return {"ok": True, "fetchedAt": now.isoformat(), "body": [],
                "source": "mikey.co.jp/mizuma_timetable"}
    status, delayed = _row_delay_min(html, now_min)
    for dep in delayed:
        body.append({
            "owl:sameAs": f"Mizuma:sta1:{dep}",
            "odpt:railway": "odpt.Railway:Mizuma",
            "odpt:trainNumber": None,
            "odpt:delay": (now_min - dep) * 60,
            "odpt:trainType": None,
            "_line_ja": "水間鉄道線",
            "_suki_line": "水間鉄道線",
            "_station": "貝塚",
            "_dest": None,
            "_direction": "下り",
        })
    return {
        "ok": True,
        "fetchedAt": now.isoformat(),
        "body": body,
        "source": "mikey.co.jp/mizuma_timetable",
    }
