"""Hokuetsu Express (北越急行 ほくほく線) live train feed.

Two server-rendered pages (Shift-JIS, refreshed ~1 min by hand):
    https://www.hokuhoku.co.jp/unkou/cellGuide.html   mobile delay summary
    https://www.hokuhoku.co.jp/unkou/pcGuide.html    per-train detail table

The PC guide's detail table is the authoritative per-train view:
    <th class="kind">列車種別</th> <th class="destination">行き先</th>
    <th class="name">列車番号</th> <th class="late">遅れ</th>
    <th class="place">現在位置</th>
and one <tr> per running train in the tbody. The mobile page instead lists
the same trains grouped as ●普通 / ●超快速 × 〈越後湯沢方面〉/〈直江津方面〉.

At 01:0x JST the last train has already returned to the yard, so the tbody
is empty and both pages honestly report nothing (their 更新タイムスタンプ
still proves the endpoint is live). We emit only rows whose 遅れ column
parses to >= 1 minute; on-time rows carry no delay signal and are dropped.

Direction: 越後湯沢方面 = 上り (toward the Jōetsu-Shinkansen connection),
直江津方面 = 下り.
"""

import re
import urllib.request

from . import common

PC_URL = "https://www.hokuhoku.co.jp/unkou/pcGuide.html"
CELL_URL = "https://www.hokuhoku.co.jp/unkou/cellGuide.html"

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

DELAY_THRESHOLD_SEC = 60


def _get(url, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Referer": "https://www.hokuhoku.co.jp/unkou/",
            })
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("cp932", "replace")
        except Exception as exc:
            last = exc
            if attempt < retries:
                import time
                time.sleep(1.5)
    raise last


def _direction(dest):
    if not dest:
        return None
    if "越後湯沢" in dest:
        return "上り"
    if "直江津" in dest:
        return "下り"
    return None


def fetch(src=None):
    src = src or common.Source()
    try:
        if src.offline:
            html = src.offline.get_text("hoku-pc.html")
        else:
            html = _get(PC_URL)
    except Exception as exc:
        print(f"hokuhoku: {type(exc).__name__}: {str(exc)[:120]}")
        return {"ok": False, "fetchedAt": common.jst_now().isoformat(),
                "body": [], "source": "hokuhoku.co.jp/unkou"}

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    body = []
    table = soup.find("div", id="detailinfo")
    if table is None:
        table = soup
    rows = table.find_all("tr")
    for row in rows:
        cells = [c.get_text(" ", strip=True)
                 for c in row.find_all(["th", "td"])]
        # 5 columns: kind, destination, name, late, place. Skip the thead
        # header row and any row that does not parse as a live train.
        if len(cells) != 5:
            continue
        kind, dest, number, late, place = cells
        if kind in ("列車種別",) or not number:
            continue
        m_delay = re.search(r"(\d+)\s*分", late)
        if not m_delay:
            continue
        delay_sec = int(m_delay.group(1)) * 60
        if delay_sec < DELAY_THRESHOLD_SEC:
            continue
        body.append({
            "owl:sameAs": f"Hokuhoku:{number}:{place}",
            "odpt:railway": "odpt.Railway:Hokuhoku.HokuhokuLine",
            "odpt:trainNumber": number,
            "odpt:delay": delay_sec,
            "odpt:trainType": kind,
            "_line_ja": "ほくほく線",
            "_suki_line": "ほくほく線",
            "_station": place or None,
            "_dest": dest or None,
            "_direction": _direction(dest),
        })
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "hokuhoku.co.jp/unkou",
    }
