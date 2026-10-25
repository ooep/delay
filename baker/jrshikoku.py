"""JR-Shikoku (四国) live train feed for the baker.

Source: the company's own position map at https://train.jr-shikoku.co.jp/
(rendered by obfuscated traindisp_ob.js; its data endpoint and JSON keys
were decoded from that script):
    GET /data/train/train.json      (Pragma: no-cache, Referer set)
    keys per train: Line, TrainNum, Type, Direction, delay, Pos, DispNum

Line codes map onto dokorail's own JR-Shikoku railway ids, so timetable
legs come from dokorail's bundles via the normal LegStore path.

IMPORTANT: JR-Shikoku suspended this feed on 2026-10-22 (official X
announcement, no restoration date); the endpoint answers 403 while the
HTML page still loads. fetch() treats any failure as "unavailable" and
the board simply omits the operator - when distribution resumes, JR-Shikoku
trains appear in data/delays.json with no code change, exactly like the
JR-East 403 case on dokorail's feed proxy.
"""

from . import common

BASE = "https://train.jr-shikoku.co.jp"
TRAIN_JSON = BASE + "/data/train/train.json"

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
    (suspended / network error / unexpected shape)."""
    try:
        data = common.get_json(
            TRAIN_JSON,
            headers={
                "Referer": BASE + "/sp.html",
                "Pragma": "no-cache",
                "Cache-Control": "no-cache",
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
            },
        )
    except Exception:
        return None
    return parse_raw(data)
