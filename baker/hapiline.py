"""Hapi-Line Fukui (ハピラインふくい) live train feed via ハピラインNAVI.

Public position map:
    GET https://trainlocation.hapi-line.co.jp/sp/   (mobile)
    GET https://trainlocation.hapi-line.co.jp/        (desktop)

The NAVI diagram overlays every in-service train on the 敦賀 ⇄ 福井 ⇄ 大聖寺
line as a number badge (e.g. 1753M, 720D, 1329M) followed by its destination
(福井行 / 敦賀行 / 金沢行 / 芦原温泉行), with a "表示時間：YYYY/MM/DD HH:MM:SS"
timestamp and an overall 運行情報 banner (運行情報なし = normal).

BLOCKED-AT-REVERSE-TIME NOTE: this runner's egress could not reach
trainlocation.hapi-line.co.jp at all (proxy CONNECT timeouts on every retry,
root / /sp/ /www variants). Only a text-extraction fetch through an internal
fetcher worked, which strips the HTML/JS source — so the exact badge markup
and whether per-train "N分遅れ" badges exist could NOT be confirmed live
(待实机抓包). The parser below scans the rendered page for train tokens
(4-digit number + M/D suffix) and emits an entry only when a delay token is
found on the same badge; otherwise the body stays [] (位置図のみで遅れ時分表示が無ければ空).
"""

import re
import urllib.request

from . import common

URL = "https://trainlocation.hapi-line.co.jp/sp/"

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

DELAY_THRESHOLD_SEC = 60

RE_TRAIN = re.compile(r"\b(\d{3,4}[MD])\b")
RE_DEST = re.compile(r"(金沢|福井|敦賀|芦原温泉|大聖寺)行")
RE_DELAY = re.compile(r"(\d+)\s*分(?:遅れ|遅延|程度)")
RE_STAMP = re.compile(r"表示時間[：:]\s*(\d{4}/\d{2}/\d{2}\s*\d{1,2}:\d{2}(?::\d{2})?)")


def _get(url, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as exc:
            last = exc
            if attempt < retries:
                import time
                time.sleep(1.5)
    raise last


def fetch(src=None):
    src = src or common.Source()
    try:
        if src.offline:
            html = src.offline.get_text("hapi-sp.html")
        else:
            html = _get(URL)
    except Exception as exc:
        print(f"hapi-line: {type(exc).__name__}: {str(exc)[:120]}")
        return {"ok": False, "fetchedAt": common.jst_now().isoformat(),
                "body": [], "source": "trainlocation.hapi-line.co.jp",
                "blocked": True}

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    stamp = RE_STAMP.search(soup.get_text(" ", strip=True))

    body = []
    seen = set()
    for node in soup.find_all(string=RE_TRAIN):
        number = RE_TRAIN.search(str(node)).group(1)
        parent_text = node.parent.get_text(" ", strip=True) if node.parent else ""
        m_delay = RE_DELAY.search(parent_text)
        if not m_delay:
            continue  # no delay badge: not emitted (宁漏不误报)
        delay_sec = int(m_delay.group(1)) * 60
        if delay_sec < DELAY_THRESHOLD_SEC or number in seen:
            continue
        seen.add(number)
        m_dest = RE_DEST.search(parent_text)
        dest = m_dest.group(1) if m_dest else None
        body.append({
            "owl:sameAs": f"HapiLine:{number}",
            "odpt:railway": "odpt.Railway:HapilineFukui.HapilineFukui",
            "odpt:trainNumber": number,
            "odpt:delay": delay_sec,
            "odpt:trainType": "普通",
            "_line_ja": "ハピラインふくい線",
            "_suki_line": "ハピラインふくい線",
            "_station": None,      # badge position unconfirmed 待实机抓包
            "_dest": dest,
            "_direction": "上り" if dest == "金沢" else (
                "下り" if dest in ("敦賀", "福井") else None),
        })
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "source_time": stamp.group(1) if stamp else None,
        "body": body,
        "source": "trainlocation.hapi-line.co.jp",
    }
