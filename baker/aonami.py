"""Aonami Line (名古屋臨海高速鉄道 あおなみ線) live train feed.

Public position map:
    GET https://trainlocation-aonamiline.jp/   (あおなみ線 列車走行位置)

The map renders every in-service train as a numbered marker (01AN .. 11AN)
on the 名古屋 ⇄ 金城ふ頭 diagram, grouped under ▲名古屋方面 / ▼金城ふ頭方面
headings, with a "M/D HH:MM 更新" timestamp. Each marker carries its
種別/行先 (e.g. 金城ふ頭ゆき 普通 / 名古屋ゆき 普通).

BLOCKED-AT-REVERSE-TIME NOTE: this runner's egress could not establish a TCP
connection to trainlocation-aonamiline.jp (proxy CONNECT timeouts on every
retry, both http/https and alternate UAs); only a text-extraction fetch
through an internal fetcher worked, which strips the HTML/JS source. The
exact marker markup and whether per-train "N分遅れ" badges exist could
therefore NOT be confirmed live — 待实机抓包. The parser below is written
tolerantly: it scans the rendered HTML for train tokens matching /XXAN/
(XX = 01..11) and emits an entry only when a delay token ("N分遅れ/N分遅延") is found
associated with that train. With no delay signal the body is [] and that
honestly reflects the source (位置図のみ、遅れ時分の表示が無ければ空 bodies).
"""

import re
import urllib.request

from . import common

URL = "https://trainlocation-aonamiline.jp/"

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

DELAY_THRESHOLD_SEC = 60

RE_TRAIN = re.compile(r"\b(\d{2}AN)\b")
RE_DELAY = re.compile(r"(\d+)\s*分(?:遅れ|遅延|程度)")
RE_STAMP = re.compile(r"(\d{1,2}/\d{1,2}\s*\d{1,2}:\d{2})\s*更新")


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
            html = src.offline.get_text("aonami-index.html")
        else:
            html = _get(URL)
    except Exception as exc:
        print(f"aonami: {type(exc).__name__}: {str(exc)[:120]}")
        # Egress blocked (observed 2026-10-28: proxy CONNECT timeout on every
        # retry). Report the block rather than fabricating trains.
        return {"ok": False, "fetchedAt": common.jst_now().isoformat(),
                "body": [], "source": "trainlocation-aonamiline.jp",
                "blocked": True}

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    stamp = RE_STAMP.search(text)
    body = []
    seen = set()
    # Tolerant scan: each train token must carry its own delay token on the
    # same marker element to be emitted (宁漏不误报).
    for node in soup.find_all(string=RE_TRAIN):
        number = RE_TRAIN.search(str(node)).group(1)
        ctx = " ".join([str(node), node.parent.get_text(" ", strip=True)
                        if node.parent else ""])
        m_delay = RE_DELAY.search(ctx)
        if not m_delay:
            continue
        delay_sec = int(m_delay.group(1)) * 60
        if delay_sec < DELAY_THRESHOLD_SEC or number in seen:
            continue
        seen.add(number)
        toward = "名古屋" if "名古屋方面" in ctx else (
            "金城ふ頭" if "金城ふ頭方面" in ctx else None)
        body.append({
            "owl:sameAs": f"Aonami:{number}",
            "odpt:railway": "odpt.Railway:Aonami.AonamiLine",
            "odpt:trainNumber": number,
            "odpt:delay": delay_sec,
            "odpt:trainType": "普通",
            "_line_ja": "あおなみ線",
            "_suki_line": "あおなみ線",
            "_station": None,      # marker position unconfirmed 待实机抓包
            "_dest": toward,
            "_direction": "上り" if toward == "名古屋" else (
                "下り" if toward == "金城ふ頭" else None),
        })
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "source_time": stamp.group(1) if stamp else None,
        "body": body,
        "source": "trainlocation-aonamiline.jp",
    }
