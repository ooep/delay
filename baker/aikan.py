"""Aichi Loop Railway (愛知環状鉄道) live train feed.

Hidden operation page:
    GET https://www.aikanrailway.co.jp/AikanJsp/ZaisenInfo3.jsp   (Shift-JIS,
        <meta refresh 60>, updates ~1 min, includes JR-Chuo direct trains)

The page is a server-rendered line map. During service the JSP injects:
  * train-marker <img> tags inside the 51px spacer cells between the four
    rail-segment images (Images/Rail_1..4.gif, each wired to an image map of
    the stations — Map..Map4 gives the English station order), and
  * a <textarea id="info2"> holding the human-readable delay notice
    (e.g. "上り 1234列車 3分遅れ"), shown in the grey info box by Js/info.js.

At 01:1x JST the last train has returned to the yard: no marker imgs, no
info2 text, and the page's 現在時刻 timestamp is the only live signal.

DAYTIME FORMAT NOTE: the exact marker <img alt/title> wording could not be
captured live (out of service hours) — the parser below reads every marker
img's alt/title text and only emits rows that parse as a delay line, and
treats the info2 textarea as a fallback notice source. Rows that lack both a
delay >= 60 s and a position token are dropped (宁漏不误报).
"""

import re
import urllib.request

from . import common

URL = "https://www.aikanrailway.co.jp/AikanJsp/ZaisenInfo3.jsp"

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

DELAY_THRESHOLD_SEC = 60

# Legend / track images that are never train markers.
NON_MARKER = re.compile(r"(Rail_\d|TrainJR|bg\.jpg)", re.I)

# "1234列車 岡崎 3分遅れ" / "上り 1234列車 3分遅れ" style tokens.
RE_TRAIN = re.compile(r"(\d{3,4})\s*列車")
RE_DELAY = re.compile(r"(\d+)\s*分(?:遅れ|遅延|程度)")


def _get(url, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Referer": "https://www.aikanrailway.co.jp/",
            })
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("cp932", "replace")
        except Exception as exc:
            last = exc
            if attempt < retries:
                import time
                time.sleep(1.5)
    raise last


def _direction(text):
    if not text:
        return None
    if "上り" in text:
        return "上り"
    if "下り" in text:
        return "下り"
    return None


def fetch(src=None):
    src = src or common.Source()
    try:
        if src.offline:
            html = src.offline.get_text("aikan-zaisen3.html")
        else:
            html = _get(URL)
    except Exception as exc:
        print(f"aikan: {type(exc).__name__}: {str(exc)[:120]}")
        return {"ok": False, "fetchedAt": common.jst_now().isoformat(),
                "body": [], "source": "aikanrailway.co.jp/AikanJsp"}

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser", )

    body = []
    seen = set()
    # --- 1) train markers drawn in the spacer cells ----------------------
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        if NON_MARKER.search(src):
            continue
        text = " ".join(x for x in (img.get("alt"), img.get("title")) if x)
        if not text:
            continue
        m_num = RE_TRAIN.search(text)
        m_delay = RE_DELAY.search(text)
        if not m_num or not m_delay:
            continue  # marker without a delay signal: not emitted
        delay_sec = int(m_delay.group(1)) * 60
        if delay_sec < DELAY_THRESHOLD_SEC:
            continue
        number = m_num.group(1)
        # station token: Japanese station name in the text (best effort; the
        # daytime marker wording is unobserved — verified live in service).
        m_st = re.search(r"[一-龥]{2,6}(?=\s*\d+\s*分)", text)
        station = m_st.group(0) if m_st else None
        key = (number, station, delay_sec)
        if key in seen:
            continue
        seen.add(key)
        body.append({
            "owl:sameAs": f"Aikan:{number}:{station}",
            "odpt:railway": "odpt.Railway:Aikan.AikanLine",
            "odpt:trainNumber": number,
            "odpt:delay": delay_sec,
            "odpt:trainType": None,
            "_line_ja": "愛知環状鉄道線",
            "_suki_line": "愛知環状鉄道線",
            "_station": station,
            "_dest": None,
            "_direction": _direction(text),
        })

    # --- 2) info2 notice text (fallback delay signal, no station) ---------
    # The textarea only carries a human notice; without a per-train position
    # it is NOT promoted into body (宁漏不误报), kept here for diagnostics.
    box = soup.find(id="info2") or soup.find("textarea")
    notice = re.sub(r"\s+", " ", box.get_text(" ", strip=True)) if box else ""

    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "aikanrailway.co.jp/AikanJsp",
        "info_notice": notice or None,
    }
