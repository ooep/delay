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
"""

import time

from . import common

API = "https://api.tokyoglance.com"
OBJECTS_URL = API + "/v2/transit/objects/train"
DELAYS_URL = API + "/v1/transit/delays"

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


def _get_envelope(src):
    """Return the parsed objects envelope, replaying offline fixtures when set."""
    if src is not None and getattr(src, "offline", None) is not None:
        return src.offline.get_json("live_objects.json")
    return common.get_json(OBJECTS_URL, headers={"User-Agent": UA,
                                               "Accept": "application/json"},
                          retries=2)


def _station_name(strings, idx):
    """Current-position station id, only when it really is a station ref."""
    if isinstance(idx, int) and 0 <= idx < len(strings) \
            and strings[idx].startswith("odpt.Station:"):
        return strings[idx]
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
    except Exception as exc:  # provider down -> honest empty, not a crash
        print(f"tokyoglance objects: {type(exc).__name__}: {str(exc)[:120]}")
        return {"ok": True, "fetchedAt": common.jst_now().isoformat(),
                "body": [], "source": source}

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
            "_station": _station_name(strings, o.get("r")),
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
