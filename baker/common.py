"""Shared helpers for the jr.suki.ing baker.

Everything talks to dokorail.jp (our own site) and uses only the stdlib so
Actions runs need no pip install.
"""

import gzip
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# DOKORAIL can be pointed at a reverse proxy (e.g. Vercel edge) to dodge
# Cloudflare's runner-IP block. Set DOKORAIL_BASE in the Action env.
DOKORAIL = os.environ.get("DOKORAIL_BASE", "https://dokorail.jp")
DOKORAIL_ORIGIN = "https://dokorail.jp"
SUKI_STATIC = "https://jr-static.suki.ing/data"
JST = timezone(timedelta(hours=9))
GEOMETRY_URL_RE = re.compile(r"/rail/geometry/sha256/[0-9a-f]{64}\.json")
TRAIN_SEARCH_HEADER = {"X-Train-Search": "1"}  # /api/* rejects calls without it
PAREN_RE = re.compile(r"\s*[(（].*$")  # strip line-name section suffixes


def http_get(url, headers=None, retries=3, timeout=30, data=None, method=None):
    # datacenter IPs with the default Python-urllib UA get challenged by
    # Cloudflare/Akamai on dokorail and the JR sites - always look like a
    # browser; per-call headers still take precedence
    base = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"}
    if headers:
        base.update(headers)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers=base, data=data,
                method=method or ("POST" if data is not None else None),
            )
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
                    req = urllib.request.Request(
                        url, headers=base, data=data,
                        method=method or ("POST" if data is not None else None),
                    )
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


def get_json(url, headers=None, retries=3, data=None, method=None):
    return json.loads(http_get(url, headers, retries=retries, data=data, method=method))


def get_text(url, headers=None, retries=3, timeout=30):
    return http_get(url, headers, retries=retries, timeout=timeout).decode("utf-8", "replace")


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

    def get_text(self, name):
        return (self.root / name).read_text(encoding="utf-8", errors="replace")


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
        # large JSON on a flaky CDN: two attempts max - a third full re-read
        # costs minutes for zero gain, and the bake degrades to no-legs anyway
        return get_json(DOKORAIL + "/rail/timetables/startup-v3-index.json", retries=2)

    def bundle(self, url_or_path):
        if self.offline:
            name = url_or_path.rsplit("/", 1)[-1]
            for cand in (name, f"bundle-{name}"):
                p = self.offline.root / cand
                if p.exists():
                    return json.loads(p.read_text())
            return None  # offline fixtures are partial; skip missing calendars
        # Rewrite absolute upstream URLs so they also flow through the proxy.
        if url_or_path.startswith(DOKORAIL_ORIGIN + "/"):
            url_or_path = DOKORAIL + url_or_path[len(DOKORAIL_ORIGIN):]
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
        except Exception as exc:
            print(f"feed {operator}: {type(exc).__name__}: {str(exc)[:160]}")
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

    def jr_east_dokotrain_feed(self):
        """JR-East どこトレ position JSON (baker/jreast_dokotrain.py).
        Offline mode replays the captured status/master/position fixtures."""
        from . import jreast_dokotrain

        return jreast_dokotrain.fetch(self)

    def jr_east_app_feed(self):
        """JR-East official app cloudrail feed: per-train delay for Shinkansen
        (baker/jreast_app.py). Registers an anonymous User-ID, then reads
        train_<line>.json (88 東北/87 上越/89 北陸/90 山形/86 秋田)."""
        from . import jreast_app

        return jreast_app.fetch(self)

    def nankai_feed(self):
        """Nankai app position JSON (baker/nankai.py). Offline mode replays
        the captured trains/lines fixtures."""
        from . import nankai

        return nankai.fetch(self)

    def nishitetsu_feed(self):
        """Nishitetsu NNR position JSON (baker/nishitetsu.py). Offline mode
        replays the captured tenjin-omuta/stopStation fixtures."""
        from . import nishitetsu

        return nishitetsu.fetch(self)

    def jr_central_feed(self):
        """JR-Central zairaisen JSON (baker/jrcentral.py). Offline mode
        replays the captured zaisenichi fixture."""
        from . import jrcentral

        return jrcentral.fetch(self)

    def keihan_feed(self):
        """Keihan zaisen-up position JSON (baker/keihan.py). Offline mode
        replays the captured trainPositionList fixture."""
        from . import keihan

        return keihan.fetch(self)

    def hanshin_feed(self):
        """Hanshin slot-board displays (baker/hanshin.py). Offline mode
        replays the captured ikisaki fixtures + masters."""
        from . import hanshin

        return hanshin.fetch(self)

    def kintetsu_feed(self):
        """Kintetsu LocationWeb server-rendered pages (baker/kintetsu.py).
        Offline mode replays captured kt-w*.html fixtures."""
        from . import kintetsu

        return kintetsu.fetch(self)

    def jr_west_guide_feed(self):
        """JR-West official train-guide JSON (baker/jrwest_guide.py).
        Offline mode replays the captured area/position/station fixtures."""
        from . import jrwest_guide

        return jrwest_guide.fetch(self)

    def odpt_feed(self, operator):
        """ODPT odpt:Train feed (baker/odpt.py) - keyless public mirror for
        Toei, keyed API for Keikyu/Tobu/YokohamaMunicipal when ODPT_KEY is
        set. Offline mode replays the odpt-<operator>.json fixture."""
        from . import odpt

        return odpt.fetch(self, operator)

    def keikyu_feed(self):
        """Keikyu app-kq.net position JSON (baker/keikyu.py). Offline mode
        replays the captured kq-train.json fixture."""
        from . import keikyu

        return keikyu.fetch(self)

    def keisei_feed(self):
        """Keisei / Shin-Keisei zaisen JSON (baker/keisei.py). Offline mode
        replays the captured keisei-traffic / keisei-matsudo fixtures."""
        from . import keisei

        return keisei.fetch(self)

    def tx_feed(self):
        """Tsukuba Express external-data trains JSON (baker/tx.py). Offline
        mode replays the captured tx-trains.json fixture."""
        from . import tx

        return tx.fetch(self)

    def tobuspapp_feed(self):
        """Tobu POINT SSR-HTML position pages (baker/tobuspapp.py). Offline
        mode replays the captured tobu-<bit>.html fixtures."""
        from . import tobuspapp

        return tobuspapp.fetch(self)

    def tokyu_feed(self):
        """Tokyu tid position JSON via the Sotetsu-app external-data backend
        (baker/tokyu.py). Offline replays the captured tokyo-*.json fixtures."""
        from . import tokyu

        return tokyu.fetch(self)

    def seibu_feed(self):
        """Seibu trainfo-api position JSON (baker/seibu.py). Offline replays
        the captured seibu-trains.json bundle."""
        from . import seibu

        return seibu.fetch(self)

    def sotetsu_feed(self):
        """Sotetsu native JSON API (baker/sotetsu.py). Offline mode replays
        the captured sotetsu-lines.json / sotetsu-operations.json fixtures."""
        from . import sotetsu

        return sotetsu.fetch(self)

    def tokyo_metro_feed(self):
        """Tokyo Metro keyless line-level status, official App API
        (baker/tokyometro_app.py, reverse-engineered from 東京メトロmy!アプリ
        v7.2.0). Anonymous register -> JWT -> /v7/app/home, covers all 9
        東京メトロ + 4 都営 lines, works from runner IPs with no key and no
        proxy. Per-train positions still need ODPT_KEY (odpt.py)."""
        from . import tokyometro_app

        return tokyometro_app.fetch(self)

    def odakyu_feed(self):
        """Odakyu WebView native JSON API (baker/odakyu.py). Offline mode
        replays the captured odakyu-trains.json fixture."""
        from . import odakyu

        return odakyu.fetch(self)

    def osakametro_feed(self):
        """Osaka Metro line-level operation status (baker/osakametro.py
        fetch_operation): the official operationinfo API carries per-line
        delay/suspension alerts with cause + time + range. Position/congestion
        stays in the standalone osakametro-congestion.json payload. Offline
        replays the captured om-operation.json / om-route.json fixtures."""
        from . import osakametro

        return osakametro.fetch_operation(self)

    def keio_feed(self):
        """Keio tid-platform web JSON (baker/keio.py). Offline mode replays
        the captured keio-traffic/position/syasyu/ikisaki fixtures."""
        from . import keio

        return keio.fetch(self)

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
