"""神戸市交通局 — Kobe Municipal Subway line-level operation status.

Source (official, keyless, server-rendered):
    https://kotsu.city.kobe.lg.jp/operation/train_operation/
-> one button per 運行系統 with an icon + label (平常運転 / 遅延あり /
運転見合わせ) and a detail dialog; a timestamp ("※2026年10月30日18時03分
現在") sits above them. The page announces only 30-minute-plus delays, so an
alert is reported at the 30-minute floor with the official text attached —
board-only, like the other line-level feeds.

The operator groups 西神・山手線 and 北神線 into one status row while
data/lines.json keeps them as two railways, so that row emits one entry per
railway. All lines normal -> empty body.

Unrecognised wording is a source failure (ok=False), never a delay:
宁漏不误报.
"""

import re

from . import common

URL = "https://kotsu.city.kobe.lg.jp/operation/train_operation/"

LINE_LEVEL_DELAY_SECONDS = 30 * 60

GROUPS = (
    (("西神", "北神"), (
        ("KobeMunicipal.SeishinYamateLineWestbound", "神戸市営地下鉄西神・山手線"),
        ("KobeMunicipal.KobeCitySubwayHokushin", "神戸市営地下鉄北神線"),
    )),
    (("海岸",), (
        ("KobeMunicipal.KaiganLineForSannomiyaHanadokeimae", "神戸市営地下鉄海岸線"),
    )),
)
SUSPEND_RE = re.compile(r"見合わせ|運休|運転中止")
ALERT_RE = re.compile(r"遅延|遅れ|乱れ|見合わせ|運休|中止")
STAMP_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日(\d{1,2})時(\d{1,2})分")
TAG_RE = re.compile(r"<[^>]+>")


def _text(fragment):
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", fragment)).strip()


def parse_status(html):
    """Return (status_by_railway, updated) — empty dict = all normal.

    Raises ValueError on a page whose blocks cannot be read."""
    stamp = STAMP_RE.search(html or "")
    updated = None
    if stamp:
        updated = "{}/{}/{} {}:{}".format(*stamp.groups())

    buttons = re.findall(
        r'<button[^>]*class="subway-info__btn[^"]*"[^>]*data-content-id="([^"]+)"[^>]*>'
        r"([\s\S]*?)</button>", html or "")
    if len(buttons) != len(GROUPS):
        raise ValueError(f"found {len(buttons)} status buttons")

    dialogs = {}
    for m in re.finditer(
            r'<(?:dialog|div)[^>]*id="([^"]+)"[^>]*>([\s\S]*?)</(?:dialog|div)>', html):
        dialogs.setdefault(m.group(1), _text(m.group(2)))

    out = {}
    for content_id, block in buttons:
        h2 = re.search(r"<h2>([\s\S]*?)</h2>", block)
        names = _text(h2.group(1)) if h2 else ""
        label = ""
        p = re.search(r"<p>([\s\S]*?)</p>", block)
        if p:
            label = _text(p.group(1))
        else:
            alt = re.search(r'alt="([^"]*)"', block)
            label = alt.group(1) if alt else ""
        group = next((rails for keys, rails in GROUPS
                      if any(k in names for k in keys)), None)
        if not group:
            raise ValueError(f"unknown line group {names!r}")
        if "平常" in label:
            continue
        if not ALERT_RE.search(label):
            raise ValueError(f"unrecognised status {label!r}")
        status = "suspended" if SUSPEND_RE.search(label) else "delayed"
        detail = dialogs.get(content_id) or label
        text = detail if label in detail else f"{label} {detail}"
        for railway, ja in group:
            out[railway] = (status, ja, text)
    return out, updated


def fetch(src=None):
    src = src or common.Source()
    fetched_at = common.jst_now()
    if src.offline:
        try:
            html = src.offline.get_text("kobe-train-operation.html")
        except Exception:
            html = None
    else:
        try:
            html = common.get_text(URL, retries=2, timeout=25)
        except Exception as exc:
            print(f"kobe train_operation: {type(exc).__name__}: {str(exc)[:120]}")
            html = None

    try:
        alerts, updated = parse_status(html)
    except (ValueError, AttributeError) as exc:
        print(f"kobe train_operation: {exc}")
        return {"ok": False, "fetchedAt": fetched_at.isoformat(), "body": [],
                "source": "kotsu.city.kobe.lg.jp", "error": "train_operation unavailable"}

    body = []
    for railway, (status, ja, text) in alerts.items():
        body.append({
            "owl:sameAs": "KobeMunicipal:" + railway.split(".", 1)[-1],
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
            "source": "kotsu.city.kobe.lg.jp"}
