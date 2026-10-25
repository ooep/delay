"""Shared helpers for the jr.suki.ing baker.

Everything talks to dokorail.jp (our own site) and uses only the stdlib so
Actions runs need no pip install.
"""

import gzip
import json
import re
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DOKORAIL = "https://dokorail.jp"
SUKI_STATIC = "https://jr-static.suki.ing/data"
JST = timezone(timedelta(hours=9))
GEOMETRY_URL_RE = re.compile(r"/rail/geometry/sha256/[0-9a-f]{64}\.json")
TRAIN_SEARCH_HEADER = {"X-Train-Search": "1"}  # /api/* rejects calls without it


def http_get(url, headers=None, retries=3, timeout=30):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.URLError as exc:
            # some runners lack the CA chain for every CDN; retry once
            # unverified (public timetable/delay data, no credentials sent)
            if isinstance(getattr(exc, "reason", None), ssl.SSLError) and attempt == 0:
                try:
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    req = urllib.request.Request(url, headers=headers or {})
                    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                        data = resp.read()
                        if resp.headers.get("Content-Encoding") == "gzip":
                            data = gzip.decompress(data)
                        return data
                except Exception as exc2:
                    last = exc2
                    if attempt < retries - 1:
                        time.sleep(2**attempt)
                    continue
            last = exc
            if attempt < retries - 1:
                time.sleep(2**attempt)
        except Exception as exc:  # noqa: BLE001 - retry any transport error
            last = exc
            if attempt < retries - 1:
                time.sleep(2**attempt)
    raise last


def get_json(url, headers=None):
    return json.loads(http_get(url, headers))


def jst_now():
    return datetime.now(JST)


class Offline:
    """Serve fetches from a directory of files captured earlier.

    Keys are file basenames; lets us test the whole pipeline against the
    fixtures captured while reverse-engineering the formats.
    """

    def __init__(self, root):
        self.root = Path(root)

    def get_json(self, name):
        return json.loads((self.root / name).read_text())


class Source:
    """Live dokorail fetcher (or offline fixture fetcher)."""

    def __init__(self, offline_dir=None):
        self.offline = Offline(offline_dir) if offline_dir else None

    def geometry_catalog(self):
        """The geometry catalog URL is baked into index.html with a sha256
        path that changes on every dokorail rebuild, so discover it."""
        if self.offline:
            return self.offline.get_json("railcatalog.json")
        html = http_get(DOKORAIL + "/").decode()
        m = GEOMETRY_URL_RE.search(html)
        if not m:
            raise RuntimeError("geometry catalog URL not found in dokorail index.html")
        return get_json(DOKORAIL + m.group(0))

    def timetable_index(self):
        if self.offline:
            return self.offline.get_json("tt-index.json")
        return get_json(DOKORAIL + "/rail/timetables/startup-v3-index.json")

    def bundle(self, url_or_path):
        if self.offline:
            name = url_or_path.rsplit("/", 1)[-1]
            for cand in (name, f"bundle-{name}"):
                p = self.offline.root / cand
                if p.exists():
                    return json.loads(p.read_text())
            return None  # offline fixtures are partial; skip missing calendars
        return get_json(url_or_path)

    def train_feed(self, operator):
        """Fetch the live train feed for one operator. Returns the parsed
        operator payload ({ok, etag, fetchedAt, body}) or None when dokorail
        refuses it (JR-East answers 403 today; when its feed is enabled the
        board picks it up with no code change)."""
        if self.offline:
            feeds = self.offline.get_json("feeds-all.json")
            return feeds.get("operators", {}).get(operator)
        qs = f"kind=train&operators={operator}&known=%7B%7D"
        try:
            feeds = get_json(
                DOKORAIL + "/api/feeds?" + qs, headers=dict(TRAIN_SEARCH_HEADER)
            )
        except Exception:
            return None
        return feeds.get("operators", {}).get(operator)

    def suki_tmaps(self):
        if self.offline:
            return self.offline.get_json("jrsuki-tmaps.json")
        return get_json(SUKI_STATIC + "/tmaps.json")

    def jr_shikoku_feed(self):
        """JR-Shikoku's own position JSON (baker/jrshikoku.py). Offline mode
        replays a captured/synthetic train.json fixture through the same
        parser as the live path."""
        from . import jrshikoku

        if self.offline:
            p = self.offline.root / "jrsk-trains.json"
            if not p.exists():
                return None
            return jrshikoku.parse_raw(json.loads(p.read_text()))
        return jrshikoku.fetch()

    def jr_hokkaido_feed(self):
        """JR-Hokkaido's own position + timetable JSON (baker/jrhokkaido.py).
        Offline mode replays the captured location/daiya/eki fixtures."""
        from . import jrhokkaido

        return jrhokkaido.fetch(self)

    def jr_kyushu_feed(self):
        """JR-Kyushu goto-mirai position JSON (baker/jrkyushu.py). Offline
        mode replays the captured route fixture."""
        from . import jrkyushu

        return jrkyushu.fetch(self)

    def offline_json(self, name):
        # BOM-aware: JR-Hokkaido files all start with a byte-order mark
        return json.loads((self.offline.root / name).read_text(encoding="utf-8-sig"))

    def suki_lines_map(self):
        if self.offline:
            return self.offline.get_json("jrsuki-lines.json")
        return get_json(SUKI_STATIC + "/lines_map.json")


# ---- decoded-bundle helpers ------------------------------------------------


def bundle_blocks(bundle):
    """v3 bundles are [3, block, null, null] where block may be absent.
    Yield every list block that carries a railway id."""
    for block in bundle[1:]:
        if isinstance(block, list) and len(block) >= 6 and isinstance(block[2], str):
            yield block


def decode_stops(stops, station_names):
    """stops: [stationIdx, seconds, dwellSec, platform, flag] *

    First stop: seconds = absolute departure (seconds from JST midnight,
    may exceed 86400 for post-midnight departures - keep as HH:MM with
    hours >= 24, the usual Japanese timetable convention).
    Later stops: seconds = running time from the previous stop's departure;
    arrival = prev dep + run, departure = arrival + dwell.
    """
    out = []
    dep = None
    for i, (idx, sec, dwell, platform, _flag) in enumerate(stops):
        if i == 0:
            dep = sec
            arr = None
        else:
            arr = dep + sec
            dep = arr + dwell
        entry = {"station": station_names[idx] if idx < len(station_names) else f"#{idx}"}
        if arr is not None:
            entry["arr"] = hhmm(arr)
        entry["dep"] = hhmm(dep)
        entry["platform"] = platform
        out.append(entry)
    return out


def hhmm(total_seconds):
    return f"{total_seconds // 3600:02d}:{total_seconds % 3600 // 60:02d}"


def dated_runs(mask_hex, anchor):
    """Dated calendar trains carry dates: [anchor, hexmask]. The mask packs
    one bit per day (4 days per hex char, low bit = earliest of the four),
    window length = len(mask) * 4 days from `anchor`. Verified against
    clean weekday / weekend service patterns."""
    from datetime import date, timedelta

    anchor_date = date.fromisoformat(anchor)
    days = []
    for i in range(len(mask_hex) * 4):
        k, j = divmod(i, 4)
        if (int(mask_hex[k], 16) >> j) & 1:
            days.append((anchor_date + timedelta(days=i)).isoformat())
    return days
