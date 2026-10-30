"""天竜浜名湖鉄道 — Tenhama line-level operation status.

Source (official, keyless, server-rendered):
    https://www.tenhama.co.jp/operation-status/
-> a single status box (class "u-font-medium text-center c-border-box") that
reads "YYYY/MM/DD HH:MM　平常通り運行しております。" when all is well. The
company only announces a disruption when a train is roughly 30+ minutes late,
so an alert is reported at the 30-minute floor with the page text attached.
Normal -> empty body (the board shows nothing for this operator).

Unrecognised wording is a source failure (ok=False), never a delay:
宁漏不误报.
"""

import re

from . import common

URL = "https://www.tenhama.co.jp/operation-status/"

LINE_LEVEL_DELAY_SECONDS = 30 * 60

RAILWAY = ("Tenhama.TenryuHamanako", "天竜浜名湖鉄道天竜浜名湖線")

STATUS_RE = re.compile(
    r'<div class="u-font-medium text-center c-border-box">([\s\S]*?)</div>')
STAMP_RE = re.compile(r"(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2})")
SUSPEND_RE = re.compile(r"見合わせ|運休|運転中止|運転を取りやめ")
ALERT_RE = re.compile(r"遅れ|乱れ|見合わせ|運休|中止|折返し|直通")
TAG_RE = re.compile(r"<[^>]+>")

# The notice below the box always mentions 遅れ ("列車の遅れが、概ね30分以上
# 発生した場合に…"), so only the status box itself is parsed.


def parse_status(html):
    """Return (status, text, updated) or None when all normal.

    "unreadable" is signalled by raising ValueError: the caller turns it into
    ok=False so a changed page never reads as a delayed line."""
    m = STATUS_RE.search(html or "")
    if not m:
        raise ValueError("status box not found")
    raw = m.group(1)
    text = re.sub(r"\s+", " ", TAG_RE.sub(" ", raw)).strip()
    stamp = STAMP_RE.search(text)
    updated = stamp.group(1) if stamp else None
    if ALERT_RE.search(text):
        status = "suspended" if SUSPEND_RE.search(text) else "delayed"
        return (status, text, updated)
    if "平常" in text:
        return None
    raise ValueError(f"unrecognised status {text[:80]!r}")


def fetch(src=None):
    src = src or common.Source()
    fetched_at = common.jst_now()
    if src.offline:
        try:
            html = src.offline.get_text("tenhama-operation-status.html")
        except Exception:
            html = None
    else:
        try:
            html = common.get_text(URL, retries=2, timeout=20)
        except Exception as exc:
            print(f"tenhama operation-status: {type(exc).__name__}: {str(exc)[:120]}")
            html = None

    try:
        parsed = parse_status(html)
    except ValueError as exc:
        print(f"tenhama operation-status: {exc}")
        return {"ok": False, "fetchedAt": fetched_at.isoformat(), "body": [],
                "source": "tenhama.co.jp", "error": "operation-status unavailable"}

    body = []
    if parsed:
        status, text, updated = parsed
        railway, ja = RAILWAY
        body.append({
            "owl:sameAs": "Tenhama:" + railway.split(".")[-1],
            "odpt:railway": "odpt.Railway:" + railway,
            "odpt:trainNumber": None,
            "odpt:delay": LINE_LEVEL_DELAY_SECONDS,
            "odpt:trainType": None,
            "_line_ja": ja,
            "_metro_status": status,
            "_metro_status_text": text[:300],
            "_metro_updated": updated,
        })
    return {"ok": True, "body": body, "fetchedAt": fetched_at.isoformat(),
            "source": "tenhama.co.jp"}
