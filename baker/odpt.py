"""ODPT (公共交通オープンデータセンター) train feed for the baker.

Covers the four operators previously fed by dokorail (Cloudflare 403s
GitHub runner IPs): Toei, Keikyu, Tobu, YokohamaMunicipal.

Two tiers:
- keyless public mirror (api-public.odpt.org) - carries some operators
  (verified live for Toei: 82 trains in transit);
- keyed API (api.odpt.org) - carries all four; free registration at
  developer.odpt.org, pass the consumer key via the ODPT_KEY environment
  (GitHub secret -> workflow env).

Response: a list of odpt:Train objects (odpt:railway, odpt:trainNumber,
odpt:delay in seconds, odpt:fromStation/toStation, odpt:trainType) - the
same shape dokorail's feed proxy passes through, so entries flow through
the standard catalog/leg/suki path unchanged.
"""

import json
import os

from . import common

KEYED_BASE = "https://api.odpt.org/api/v4"
PUBLIC_BASE = "https://api-public.odpt.org/api/v4"

DELAY_THRESHOLD_MIN = 1


def _j(src, offline_name, url):
    if src.offline:
        try:
            return src.offline_json(offline_name)
        except Exception:
            return None
    try:
        return common.get_json(url)
    except Exception as exc:
        print(f"odpt {offline_name}: {type(exc).__name__}: {str(exc)[:120]}")
        return None


def fetch(src=None, operator="Toei"):
    src = src or common.Source()
    key = os.environ.get("ODPT_KEY", "").strip()
    if key:
        base, suffix = KEYED_BASE, f"&acl:consumerKey={key}"
    else:
        base, suffix = PUBLIC_BASE, ""
        if operator != "Toei":
            # public mirror carries only a subset; without a key the rest
            # answer empty - note it once per run
            pass
    url = f"{base}/odpt:Train?odpt:operator=odpt.Operator:{operator}{suffix}"
    trains = _j(src, f"odpt-{operator}.json", url)
    if not isinstance(trains, list):
        return None

    body = []
    for t in trains:
        if not isinstance(t, dict):
            continue
        number = t.get("odpt:trainNumber")
        delay = t.get("odpt:delay")
        if not number or delay is None:
            continue
        try:
            delay = int(delay)
        except (TypeError, ValueError):
            continue
        if delay < DELAY_THRESHOLD_MIN * 60:
            continue
        body.append(
            {
                "owl:sameAs": t.get("owl:sameAs") or f"odpt:{operator}.{number}",
                "odpt:railway": t.get("odpt:railway") or "",
                "odpt:trainNumber": str(number),
                "odpt:delay": delay,
                "odpt:trainType": t.get("odpt:trainType"),
                "odpt:fromStation": t.get("odpt:fromStation"),
                "odpt:toStation": t.get("odpt:toStation"),
                "odpt:carComposition": t.get("odpt:carComposition"),
            }
        )
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": ("api.odpt.org (keyed)" if key else "api-public.odpt.org"),
    }
