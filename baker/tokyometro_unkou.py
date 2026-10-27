"""Tokyo Metro (東京メトロ) keyless line-level operation status feed.

Source: the official per-line service-status page
    https://www.tokyometro.jp/unkou/history/{line}.html
published for all nine 東京メトロ lines (銀座/丸ノ内/日比谷/東西/千代田/
有楽町/半蔵門/南北/副都心). No ODPT key required - pure HTML.

What it carries:
  - current line status (平常運転 / 【一部列車遅延】 / 【ダイヤ乱れ】 /
    運転見合わせ) plus the status text and publish time;
  - the site only announces delays of 5 minutes or more per line, so a
    delayed line is reported at the 5-minute floor with the exact status
    text attached (line-level, no train numbers).

Access caveat: tokyometro.jp is fronted by Akamai which 403s GitHub
runner IPs. Set TOKYOMETRO_BASE (e.g. a Vercel/Cloudflare reverse proxy)
to route around the block; unset, the fetcher hits tokyometro.jp directly
and degrades gracefully (no data this run) on 403.

With ODPT_KEY set, baker/odpt.py already delivers real per-train
東京メトロ positions/delays from api.odpt.org; this feed is the keyless
fallback that works with no key at all.
"""

import os
import re

from . import common

DEFAULT_BASE = "https://www.tokyometro.jp"
LINES = [
    # (page slug, line name)
    ("ginza", "銀座線"),
    ("marunouchi", "丸ノ内線"),
    ("hibiya", "日比谷線"),
    ("touzai", "東西線"),
    ("chiyoda", "千代田線"),
    ("yurakucho", "有楽町線"),
    ("hanzomon", "半蔵門線"),
    ("namboku", "南北線"),
    ("fukutoshin", "副都心線"),
]

DELAY_THRESHOLD_SECONDS = 5 * 60  # the site's own 5-minute announcement floor
_STATUSES = [
    ("suspended", ("運転を見合わせ", "運転見合わせ", "運転を再開")),
    ("disrupted", ("ダイヤ乱れ", "ダイヤが乱れ", "ダイヤを乱し")),
    ("delayed", ("一部列車遅延", "一部の列車に遅れ", "遅延", "遅れが出")),
]
_STATE_RE = re.compile(
    r"(\d{1,2}月\d{1,2}日\s*\d{2}:\d{2})\s*現在\s*情報更新(.{0,500}?)(?=###|\n###|履歴|注意1|過去35日間|$)",
    re.DOTALL,
)
# Akamai 403s datacenter IPs on tokyometro.jp (verified on GitHub runners).
# Fallback chain per page: TOKYOMETRO_BASE (user reverse proxy) -> direct ->
# public raw proxies (short timeout, so a dead proxy costs ~10s, not 30).
PROXY_TEMPLATES = (
    "https://api.allorigins.win/raw?url={url}",
    "https://api.codetabs.com/v1/proxy?quest={url}",
)


def _page_text(src, base, slug):
    """Try the configured base, then direct, then public proxies. Returns
    (html, via) or (None, last_error)."""
    from urllib.parse import quote

    url = f"{base}/unkou/history/{slug}.html"
    candidates = [url]
    if base != DEFAULT_BASE:
        candidates.append(f"{DEFAULT_BASE}/unkou/history/{slug}.html")
    proxies = [t.format(url=quote(u, safe="")) for u in candidates for t in PROXY_TEMPLATES]
    last = None
    for u in candidates + proxies:
        timeout = 10 if "allorigins" in u or "codetabs" in u else None
        try:
            return common.get_text(u, timeout=timeout), u
        except Exception as exc:  # noqa: BLE001 - any miss degrades a hop
            last = exc
    return None, last


def _parse_status_text(s):
    s = re.sub(r"\s+", " ", s).strip()
    return s[:300]


def _classify(status_text):
    for status, needles in _STATUSES:
        for n in needles:
            if n in status_text:
                return status
    return "normal"


def fetch(src=None):
    src = src or common.Source()
    base = os.environ.get("TOKYOMETRO_BASE", "").strip() or DEFAULT_BASE
    body = []
    fetched_at = common.jst_now()
    for slug, ja in LINES:
        if src.offline:
            try:
                html = src.offline.get_text(f"tm-{slug}.html")
            except Exception:
                continue
        else:
            html, _via = _page_text(src, base, slug)
            if html is None:
                print(f"tokyometro {slug}: all endpoints failed ({str(_via)[:120]})")
                continue
        if not html:
            continue
        m = _STATE_RE.search(html)
        status_text = _parse_status_text(m.group(2)) if m else ""
        status = _classify(status_text)
        if status == "normal":
            continue  # board only carries delayed trains
        body.append(
            {
                "owl:sameAs": f"TokyoMetro:{slug}",
                "odpt:railway": f"odpt.Railway:TokyoMetro.{slug.capitalize()}",
                "odpt:trainNumber": None,
                "odpt:delay": DELAY_THRESHOLD_SECONDS,
                "odpt:trainType": None,
                "odpt:fromStation": None,
                "odpt:toStation": None,
                "_line_ja": ja,
                "_metro_status": status,
                "_metro_status_text": status_text,
                "_metro_updated": m.group(1) if m else None,
            }
        )
    return {"ok": True, "body": body, "fetchedAt": fetched_at.isoformat()}
