"""仙台市交通局 — Sendai Municipal Subway line-level operation status.

Source (official, keyless, server-rendered):
    https://www.kotsu.city.sendai.jp/unkou
-> one <li class="nanboku|tozai"> block per subway line with a status cell;
the normal cell reads 平常運行. Alerts replace that text and the page only
announces them when a delay reaches 30 minutes ("地下鉄に30分以上の遅れが
発生または見込まれる場合"), so an alert is reported at the 30-minute floor —
not the 5-minute floor of the Tokyo/Osaka feeds — with the page text attached.
All lines normal -> empty body (the board shows nothing for this operator).

Unrecognised text is treated as a source failure (ok=False) instead of a
delay: 宁漏不误报. Parse failures never fabricate a delayed line.
"""

import re

from . import common

URL = "https://www.kotsu.city.sendai.jp/unkou"

LINE_LEVEL_DELAY_SECONDS = 30 * 60

LINES = {
    "nanboku": ("SendaiMunicipal.SendaiSubwayNamboku", "仙台市地下鉄南北線"),
    "tozai": ("SendaiMunicipal.SendaiSubwayTozai", "仙台市地下鉄東西線"),
}
SUSPEND_RE = re.compile(r"見合わせ|運休|運転中止|運転を取りやめ")
ALERT_RE = re.compile(r"遅れ|乱れ|見合わせ|運休|中止|折返し|直通|平常運行ではありません|平常運転ではありません")
TAG_RE = re.compile(r"<[^>]+>")


def _text(fragment):
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", fragment)).strip()


def parse_status(html):
    """Return {rosen: (status, text)} for non-normal subway lines.

    Empty dict = all normal. None = the page could not be read as expected
    (unknown wording or missing blocks) — the caller reports the feed dead."""
    out = {}
    seen = 0
    for m in re.finditer(r'<li class="(nanboku|tozai)">([\s\S]*?)</li>', html):
        rosen, block = m.group(1), m.group(2)
        dd = re.search(r"<dd[^>]*>([\s\S]*?)</dd>", block)
        if not dd:
            return None
        seen += 1
        cell = _text(dd.group(1))
        if ALERT_RE.search(cell):
            out[rosen] = ("suspended" if SUSPEND_RE.search(cell) else "delayed", cell)
        elif "平常" in cell:
            continue
        else:
            print(f"sendai {rosen}: unrecognised status {cell[:80]!r}")
            return None
    if seen != len(LINES):
        print(f"sendai: found {seen}/{len(LINES)} subway blocks")
        return None
    return out


def fetch(src=None):
    src = src or common.Source()
    fetched_at = common.jst_now()
    if src.offline:
        try:
            html = src.offline.get_text("sendai-unkou.html")
        except Exception:
            html = None
    else:
        try:
            html = common.get_text(URL, retries=2, timeout=20)
        except Exception as exc:
            print(f"sendai unkou: {type(exc).__name__}: {str(exc)[:120]}")
            html = None
    statuses = parse_status(html) if html else None
    if statuses is None:
        return {"ok": False, "fetchedAt": fetched_at.isoformat(), "body": [],
                "source": "kotsu.city.sendai.jp", "error": "unkou page unavailable"}

    body = []
    for rosen, (status, text) in statuses.items():
        railway, ja = LINES[rosen]
        body.append({
            "owl:sameAs": f"SendaiMunicipal:{rosen}",
            "odpt:railway": "odpt.Railway:" + railway,
            "odpt:trainNumber": None,
            "odpt:delay": LINE_LEVEL_DELAY_SECONDS,
            "odpt:trainType": None,
            "_line_ja": ja,
            "_metro_status": status,
            "_metro_status_text": text[:300],
            "_metro_updated": None,
        })
    return {"ok": True, "body": body, "fetchedAt": fetched_at.isoformat(),
            "source": "kotsu.city.sendai.jp"}
