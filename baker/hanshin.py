"""Hanshin (阪神電鉄) live train feed for the baker.

Source: the 運行状況 app's display files (no key; 28 files, 019 missing):
    https://apl.hanshin.co.jp/data/ikisaki_<NNN>.json
Masters: apl.hanshin.co.jp/5/json/{station,syasyu,line,direction}.json

The feed is slot-board style per display group: forinf[].trninf[] with
{no: display order (NOT a train number), typ: type code, bnd: destination
station code, dly: delay minutes (string, "0" when on time), dlyflg,
stp: next-stops text ("尼崎、甲子園、…" / "各駅に停車します" /
"次は、大阪梅田終点です"), tim: time at this display, pos}.

The same physical train appears at several displays - dedupe by
(bnd, typ, stp, tim, dly). Rush-hour validation: 189 slots -> 131 unique
trains network-wide (本線/なんば線/武庫川線/神戸高速直通/山陽直通 - bnd
115=山陽姫路 confirms Sanyo through services ride in this feed).

No train numbers are published, so mapping is ID-less: the first station
of stp anchors the position-time engine (baker/delays.find_leg_by_position).
"""

from . import common

BASE = "https://apl.hanshin.co.jp"
DATA_URL = BASE + "/data/ikisaki_{:03d}.json"
MASTER_URL = BASE + "/5/json/{}.json"
IKISAKI_IDS = [i for i in range(1, 31) if i != 19]

DELAY_THRESHOLD_MIN = 1

# dokorail railways the position engine may resolve into: Hanshin proper
# plus the through partners whose trains appear in this feed
HANSHIN_RAILWAYS = [
    "Hanshin.Main",
    "Hanshin.Namba",
    "Hanshin.Mukogawa",
    "KobeRapid.TransitRailwayTozai",
    "Sanyo.Main",
    "Sanyo.Aboshi",
]

_STP_NEXT = "次は、"


def _parse_stp_stations(stp):
    """Ordered upcoming stations from the stp text ("尼崎、甲子園、…" /
    "次は、大阪梅田終点です"); [] when the text carries no station names.
    The first entries double as the position anchor and - being ordered -
    as a direction filter for leg matching."""
    if not stp:
        return []
    if _STP_NEXT in stp:
        rest = stp.split(_STP_NEXT, 1)[1]
        for stopper in ("終点です", "です", "に停車します"):
            if stopper in rest:
                rest = rest.split(stopper)[0]
        stp = rest
    out = []
    for tok in stp.replace("・", "、").split("、"):
        tok = tok.strip()
        if not tok or "停車" in tok or "です" in tok or "ます" in tok:
            continue
        out.append(tok)
    return out


def _offline_json(src, name):
    try:
        return src.offline_json(name)
    except Exception:
        return None


def fetch(src=None):
    src = src or common.Source()
    if src.offline:
        station_master = (_offline_json(src, "hs-station.json") or {}).get("station") or []
        syasyu = (_offline_json(src, "hs-syasyu.json") or {}).get("syasyu") or []
        displays = []
        for i in IKISAKI_IDS:
            d = _offline_json(src, f"hs-all/{i:03d}.json")
            if isinstance(d, dict):
                displays.append(d)
    else:
        try:
            station_master = (common.get_json(MASTER_URL.format("station")) or {}).get("station") or []
            syasyu = (common.get_json(MASTER_URL.format("syasyu")) or {}).get("syasyu") or []
        except Exception as exc:
            print(f"hanshin masters: {type(exc).__name__}: {str(exc)[:100]}")
            return None
        displays = []
        for i in IKISAKI_IDS:
            try:
                d = common.get_json(DATA_URL.format(i), headers={"Referer": BASE + "/5/html/index.html"})
            except Exception:
                continue
            if isinstance(d, dict):
                displays.append(d)

    stations = {str(s.get("sta_code")): (s.get("name") or "").replace("\u3000", "") for s in station_master}
    types = {str(s.get("code")): s.get("name") for s in syasyu}

    body = []
    seen = set()
    for d in displays:
        for fi in d.get("forinf") or []:
            for t in fi.get("trninf") or []:
                if not isinstance(t, dict) or t.get("dsp") == "0":
                    continue
                try:
                    delay_min = int(str(t.get("dly") or "0") or 0)
                except ValueError:
                    delay_min = 0
                sig = (t.get("bnd"), t.get("typ"), t.get("stp"), t.get("tim"), t.get("dly"))
                if sig in seen:
                    continue
                seen.add(sig)
                if delay_min < DELAY_THRESHOLD_MIN:
                    continue
                dest = stations.get(str(t.get("bnd")))
                upcoming = _parse_stp_stations(t.get("stp"))
                body.append(
                    {
                        # no published number: a stable display id instead
                        "owl:sameAs": f"Hanshin:{t.get('bnd')}.{t.get('typ')}.{t.get('tim')}",
                        "odpt:railway": "odpt:Railway:Hanshin",
                        "odpt:trainNumber": None,
                        "odpt:delay": delay_min * 60,
                        "odpt:trainType": types.get(str(t.get("typ"))),
                        "_railways": HANSHIN_RAILWAYS,
                        "_line_ja": "阪神本線",
                        "_suki_line": "阪神本線",
                        "_station": upcoming[0] if upcoming else None,
                        "_upcoming": upcoming[:4],
                        "_dest": dest,
                        "_tim": t.get("tim"),
                    }
                )
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "apl.hanshin.co.jp",
    }
