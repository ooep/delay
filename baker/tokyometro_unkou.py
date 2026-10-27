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
# Akamai 403s datacenter IPs on tokyometro.jp (verified on GitHub runners and
# locally). Public raw proxies (allorigins/codetabs) were probed but are not
# reachable from the runner either, so the only route is TOKYOMETRO_BASE - a
# user-deployed reverse proxy (Vercel/Cloudflare). Unset, the fetcher hits
# tokyometro.jp directly, 403s fast and yields no data this run (no slow
# fallback, so the bake stays quick and other feeds are unaffected).


def _page_text(src, base, slug):
    """Try the configured base, then direct. Returns (html, via) or
    (None, last_error)."""
    url = f"{base}/unkou/history/{slug}.html"
    candidates = [url]
    if base != DEFAULT_BASE:
        candidates.append(f"{DEFAULT_BASE}/unkou/history/{slug}.html")
    last = None
    for u in candidates:
        # Hard timeout: direct to tokyometro.jp often hangs on Akamai (TCP
        # stall, not a clean 403) from runner IPs, which was stalling the
        # whole bake for 4+ minutes. retries=1 so a dead hop moves on fast.
        timeout = 8
        try:
            return common.get_text(u, retries=1, timeout=timeout), u
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
    fetched_at = common.jst_now()

    def _one(slug_ja):
        slug, ja = slug_ja
        if src.offline:
            try:
                html = src.offline.get_text(f"tm-{slug}.html")
            except Exception:
                return None
        else:
            html, _via = _page_text(src, base, slug)
            if html is None:
                print(f"tokyometro {slug}: all endpoints failed ({str(_via)[:120]})")
                return None
        if not html:
            return None
        m = _STATE_RE.search(html)
        status_text = _parse_status_text(m.group(2)) if m else ""
        status = _classify(status_text)
        if status == "normal":
            return None
        return {
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

    # 9 lines concurrent, but bound the whole feed: worst-case per line is
    # direct(8s)+allorigins(6s)+codetabs(6s)=20s; give a little headroom so
    # the whole feed never holds up the bake主体.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    body = []
    with ThreadPoolExecutor(max_workers=9) as ex:
        futs = {ex.submit(_one, s): s for s in LINES}
        for f in as_completed(futs, timeout=25):
            try:
                r = f.result()
            except Exception:
                r = None
            if r is not None:
                body.append(r)
    return {"ok": True, "body": body, "fetchedAt": fetched_at.isoformat()}
