"""JR-Shikoku (四国) live train feed for the baker.

Source: the company's own position map at https://train.jr-shikoku.co.jp/
(obfuscated traindisp_ob.js; endpoints decoded from that script and live
browser XHR capture):

    GET /g?arg1=train&arg2=train      (Cookie: user_rule=true, Referer set)
        -> bare list, keys per train: Line, TrainNum, Type, Direction,
           delay, Pos (human-readable location text), DispNum

Access requires the site's user-agreement consent: the root page gates on a
#modal_rule dialog whose 「はい」 button runs UseRuleYesProc() which sets
`user_rule=true` (client-side cookie) - requests must send that cookie or
the server answers 403. The old /data/train/*.json paths stayed 403 after
the 2026-10-22 suspension; the /g CGI gateway is the live one (confirmed
2026-10-26).

delay is minutes when numeric; "入線"/"発車" are position statuses (treat
as 0). Line codes map onto dokorail's own JR-Shikoku railway ids, so
timetable legs come from dokorail's bundles via the normal LegStore path.
"""

from . import common

BASE = "https://train.jr-shikoku.co.jp"
GATEWAY_URL = BASE + "/g?arg1=train&arg2=train"
CONSENT_HEADERS = {
    "Cookie": "user_rule=true",
    "Referer": BASE + "/",
}

# JR-Shikoku line codes (from the map script / page assets) -> dokorail railway ids
LINE_MAP = {
    "yosan": "JR-Shikoku.Yosan",
    "uwajima": "JR-Shikoku.Yosan",
    "uwajima2": "JR-Shikoku.Yosan",
    "dosan": "JR-Shikoku.Dosan",
    "koutoku": "JR-Shikoku.Kotoku",
    "tokushima": "JR-Shikoku.Tokushima",
    "naruto": "JR-Shikoku.Naruto",
    "seto": "JR-Shikoku.HonshiBisan",
    "setoohashi": "JR-Shikoku.HonshiBisan",
    "mugi": "JR-Shikoku.Mugi",
}


def _railway(line_code):
    if not line_code:
        return None
    code = str(line_code).split("_")[0]  # yosan_u -> yosan
    return LINE_MAP.get(code) or LINE_MAP.get(str(line_code))


def _delay_to_seconds(raw):
    """The map shows "+N" minutes; tolerate str/int/None shapes."""
    if raw in (None, "", "-"):
        return 0
    try:
        return int(float(str(raw).lstrip("+"))) * 60
    except ValueError:
        return 0


def parse_raw(data):
    """Raw train.json -> dokorail-feed-shaped payload (or None if the
    shape is not recognized)."""
    trains = data.get("train") if isinstance(data, dict) else data
    if not isinstance(trains, list):
        return None
    body = []
    for t in trains:
        if not isinstance(t, dict):
            continue
        railway = _railway(t.get("Line") or t.get("line"))
        number = t.get("TrainNum") or t.get("TrainNumber")
        if not railway or not number:
            continue
        body.append(
            {
                "owl:sameAs": f"JR-Shikoku:{railway}.{number}",
                "odpt:railway": "odpt.Railway:" + railway,
                "odpt:trainNumber": str(number),
                "odpt:delay": _delay_to_seconds(t.get("delay")),
                "odpt:fromStation": None,
                "odpt:toStation": None,
                "odpt:trainType": None,
                "odpt:carComposition": None,
                "_station": t.get("Pos") or None,  # human-readable position text
            }
        )
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "train.jr-shikoku.co.jp",
    }


def fetch():
    """Returns a dokorail-feed-shaped payload, or None when unavailable
    (consent cookie rejected / network error / unexpected shape)."""
    try:
        data = common.get_json(GATEWAY_URL, headers=dict(CONSENT_HEADERS))
    except Exception as exc:
        print(f"jrshikoku feed: {type(exc).__name__}: {str(exc)[:120]}")
        return None
    return parse_raw(data)
