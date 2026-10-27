"""Tokyo Metro (東京メトロ) keyless official App-API line status feed.

Reverse-engineered from the 東京メトロmy!アプリ v7.2.0 APK (com.tokyometro,
Flutter + navitime backend). No ODPT key, no reverse proxy, no Akamai issue:
the App backend answers GitHub runner IPs fine.

Flow (all keyless, anonymous):
  POST https://api-mahbpc.tokyometro-myapp.navitime.jp/v7/users/register
      {"os":"android"}  -> {"accessToken": <JWT>}   (free, no signup)
  GET  .../v7/app/home  (Authorization: Bearer <token>)
      -> {"announcements":[{LineId, StatusName, DelayTime, StopTime,
                            Announcement, Title, Body}, ...]}

Covers all 9 東京メトロ lines + 4 都営 (Toei) lines in one call. StatusName:
平常運転 / 一部列車遅延 / ダイヤ乱れ etc. DelayTime/StopTime are severity
tiers, not minutes (they are non-zero even during normal service), so a
delayed line is reported at the 5-minute announcement floor like the
official unkou pages, with the exact status text attached. Line-level only
(no train numbers) - per-train positions still need ODPT_KEY (odpt.py) or
the App's private per-train endpoint.
"""

import json
import os
import time

from . import common

BASE = "https://api-mahbpc.tokyometro-myapp.navitime.jp/v7"
DELAY_THRESHOLD_SECONDS = 5 * 60  # the site's 5-minute announcement floor
_TOKEN_TTL = 60 * 60  # JWT exp is far future; refresh at most hourly
_line_ja = {
    "ginza": "銀座線", "marunouchi": "丸ノ内線", "hibiya": "日比谷線",
    "tozai": "東西線", "chiyoda": "千代田線", "yurakucho": "有楽町線",
    "hanzomon": "半蔵門線", "namboku": "南北線", "fukutoshin": "副都心線",
    "asakusa": "都営浅草線", "mita": "都営三田線", "shinjuku": "都営新宿線",
    "oedo": "都営大江戸線",
}
_TOKEN = None
_TOKEN_AT = 0.0


def _token():
    """Anonymous JWT via users/register, cached per process."""
    global _TOKEN, _TOKEN_AT  # noqa: PLW0603
    if _TOKEN and time.monotonic() - _TOKEN_AT < _TOKEN_TTL:
        return _TOKEN
    try:
        body = common.get_json(
            f"{BASE}/users/register",
            headers={"Content-Type": "application/json"},
            data=b'{"os":"android"}',
        )
        t = body.get("accessToken")
        if t:
            _TOKEN, _TOKEN_AT = t, time.monotonic()
            return t
    except Exception as exc:
        print(f"tokyometro register: {type(exc).__name__}: {str(exc)[:120]}")
    return None


def _status(text):
    for needle in ("運転を見合わせ", "運転見合わせ", "ダイヤ乱れ", "一部列車遅延",
                   "一部の列車に遅れ", "遅延", "遅れが出"):
        if needle in text:
            return needle
    return "normal"


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        try:
            data = src.offline_json("tm-app-home.json")
        except Exception:
            return None
    else:
        token = _token()
        if not token:
            return None
        try:
            data = common.get_json(
                f"{BASE}/app/home", headers={"Authorization": f"Bearer {token}"}
            )
        except Exception as exc:
            print(f"tokyometro app/home: {type(exc).__name__}: {str(exc)[:120]}")
            return None
    if not isinstance(data, dict):
        return None
    body = []
    fetched_at = common.jst_now()
    for a in data.get("announcements") or []:
        if not isinstance(a, dict):
            continue
        lid = a.get("LineId") or ""
        status_text = " ".join(
            str(a.get(k) or "").strip()
            for k in ("StatusName", "Announcement", "Title", "Body")
        )
        s = _status(status_text)
        if s == "normal":
            continue  # board only carries delayed trains
        body.append(
            {
                "owl:sameAs": f"TokyoMetroApp:{lid}",
                "odpt:railway": f"odpt.Railway:TokyoMetro.{lid.capitalize()}",
                "odpt:trainNumber": None,
                "odpt:delay": DELAY_THRESHOLD_SECONDS,
                "odpt:trainType": None,
                "odpt:fromStation": None,
                "odpt:toStation": None,
                "_line_ja": _line_ja.get(lid) or lid,
                "_metro_status": s,
                "_metro_status_text": status_text[:300],
            }
        )
    return {"ok": True, "body": body, "fetchedAt": fetched_at.isoformat()}
