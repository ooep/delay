"""Tokyo Glance (tokyoglance.com) keyless real-time mirror — shared helper.

Reverse-engineered 2026-10-27/28. Tokyo Glance mirrors ODPT TrainTimetable
positions and serves them keyless (no consumer key), mobile UA enough:

  GET https://api.tokyoglance.com/v2/transit/objects/train
    -> {"generatedAt","kind":"train",
        "strings":[...interned strings...],
        "objects":[{
             "id":"odpt.TrainTimetable:<Operator>.<Line>.<trainNumber>.<Day>",
             "l":<strings idx -> odpt.Railway:<op>.<line>>,
             "n":"<trainNumber>",
             "r":<strings idx -> current odpt.Station (or Railway between stops)>,
             "d":<strings idx -> odpt.RailDirection:...>,
             "s":<delay seconds — absent or 0 = on time>,
             "g":[[stationIdx, unixTs, stationIdx, unixTs], ...]}]}
  GET https://api.tokyoglance.com/v1/transit/delays
    -> {"lateThresholdSeconds":300, "modes":{...}, "lines":[...]}

Real-time proof (2026-10-27): two samples 100 s apart moved every in-service
train's interval legs (688 trains common, 64 entered / 98 left the board).
Per-train delay = object.s (app.js: `delaySeconds = object?.s`, compared
against `lateThresholdSeconds` from the delays endpoint).

We emit only trains with odpt:delay >= 60 s per the wave contract. Late-night
snapshots legitimately have an empty body — that is honest, not a failure.

GitHub runner IPs get a blanket 403 here (verified 2026-10-28..30), so CI
points TOKYOGLANCE_BASE at the suki-jp-relay Worker (Tokyo egress, OIDC).
The envelope is fetched once per bake and shared by all ~30 TG modules.
"""

import os
import threading
import time

from . import common

ORIGIN = "https://api.tokyoglance.com"
BASE = os.environ.get("TOKYOGLANCE_BASE", ORIGIN).rstrip("/")
OBJECTS_URL = BASE + "/v2/transit/objects/train"
DELAYS_URL = BASE + "/v1/transit/delays"

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
      "Mobile/15E148 Safari/604.1")

EMIT_DELAY_SEC = 60  # contract threshold; TG's own board uses 300 s

# odpt.RailDirection token -> Japanese direction label
_DIRECTION_JA = {
    "Inbound": "上り", "Outbound": "下り",
    "Northbound": "上り", "Southbound": "下り",
    "Eastbound": "下り", "Westbound": "上り",
    "InnerLoop": "内回り", "OuterLoop": "外回り",
}


_ENVELOPE_TTL = 120.0   # one envelope per bake; modules run concurrently
_ERROR_TTL = 45.0       # don't let 30 waiters each retry a failing provider
_envelope_lock = threading.Lock()
_envelope_cache = {"env": None, "err": None, "at": 0.0}


def _auth_headers():
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if BASE != ORIGIN:
        key = common.jp_relay_key()
        if key:
            headers["X-Relay-Key"] = key
        else:
            print("tokyoglance: relay configured but no gate key")
        token = common.jp_relay_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        else:
            print("tokyoglance: relay configured but no OIDC token")
    return headers


def _get_envelope(src):
    """Return the parsed objects envelope, replaying offline fixtures when set."""
    if src is not None and getattr(src, "offline", None) is not None:
        return src.offline.get_json("live_objects.json")
    with _envelope_lock:
        age = time.monotonic() - _envelope_cache["at"]
        fresh = _envelope_cache["env"] is not None and age < _ENVELOPE_TTL
        stale_err = _envelope_cache["err"] is not None and age < _ERROR_TTL
        if fresh:
            return _envelope_cache["env"]
        if stale_err:
            raise _envelope_cache["err"]
        try:
            env = common.get_json(OBJECTS_URL, headers=_auth_headers(),
                                  retries=2)
        except Exception as exc:
            err = RuntimeError(f"{type(exc).__name__}: {str(exc)[:160]}")
            _envelope_cache.update(env=None, err=err, at=time.monotonic())
            raise err from exc
        _envelope_cache.update(env=env, err=None, at=time.monotonic())
        return env


def _station_name(strings, idx, line_ref):
    """Current-position station id, only when it belongs to the same operator.

    The mirror currently fills `r` with one foreign placeholder (a JR-East
    Narita Airport station) for every train, so a ref from another operator
    is treated as absent instead of being fed to the position matcher.
    """
    if isinstance(idx, int) and 0 <= idx < len(strings):
        ref = strings[idx]
        op = line_ref.split(":", 1)[-1].split(".", 1)[0]
        if ref.startswith("odpt.Station:" + op + "."):
            return ref
    return None


def _direction_ja(strings, idx):
    if not isinstance(idx, int) or idx >= len(strings):
        return None
    ref = strings[idx]
    if not ref.startswith("odpt.RailDirection:"):
        return None
    token = ref.rsplit(":", 1)[-1]
    return _DIRECTION_JA.get(token, token)


def fetch(railways, src=None, operator="TG", source="api.tokyoglance.com"):
    """Build a fetch() result for one operator.

    railways: {odpt.Railway:X.Y: "日本語線名"} — every object whose l-ref
    matches one of these lines belongs to this feed.
    """
    body = []
    matched = 0
    try:
        env = _get_envelope(src)
    except Exception as exc:  # provider down -> "unavailable", never "all on time"
        print(f"tokyoglance objects via {BASE}: {str(exc)[:160]}")
        return {"ok": False, "fetchedAt": common.jst_now().isoformat(),
                "body": [], "source": source, "error": str(exc)[:200]}

    strings = env.get("strings") or []
    for o in env.get("objects") or []:
        line_ref = strings[o["l"]] if isinstance(o.get("l"), int) \
            and o["l"] < len(strings) else None
        if line_ref not in railways:
            continue
        matched += 1
        delay = int(o["s"]) if isinstance(o.get("s"), (int, float)) else 0
        if delay < EMIT_DELAY_SEC:
            continue  # on-time (or below threshold) — not output
        ja = railways[line_ref]
        body.append({
            "owl:sameAs": f"{operator}:{line_ref}:{o.get('n')}",
            "odpt:railway": line_ref,
            "odpt:trainNumber": o.get("n"),
            "odpt:delay": delay,
            "odpt:trainType": None,  # envelope carries no train type
            "_line_ja": ja,
            "_suki_line": ja,
            "_station": _station_name(strings, o.get("r"), line_ref),
            "_dest": None,          # envelope carries no destination station
            "_direction": _direction_ja(strings, o.get("d")),
        })
    print(f"{operator}: {matched} in-service objects on "
          f"{sorted(railways)}, {len(body)} delayed >= {EMIT_DELAY_SEC} s "
          f"(envelope {env.get('generatedAt')})")
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": source,
    }
