"""Manyo Line (万葉線, Takaoka tram) live feed via the とやまロケーションシステム
buscatch rt3 backend. See baker/_buscatch.py for the protocol reverse-
engineering notes.

Operator group: id_name="t-bus", rosen_group_id=1971, one keito (549) with
two directions (上り 高岡駅方面 / 下り 越ノ潟方面).
"""

from . import common
from . import _buscatch

ROSEN_GROUP_ID = 1971
ID_NAME = "t-bus"

# (keito_id, rosen_id, keito_name, direction)
LINES = [
    {"keito_id": 549, "rosen_id": 11886, "keito_name": "万葉線",
     "direction": "下り"},   # 下り(越ノ潟方面)
    {"keito_id": 549, "rosen_id": 11885, "keito_name": "万葉線",
     "direction": "上り"},   # 上り(高岡駅方面)
]


def fetch(src=None):
    src = src or common.Source()
    body = _buscatch.fetch_feed(
        ID_NAME, ROSEN_GROUP_ID, LINES,
        railway_prefix="odpt.Railway:Manyo",
        source_domain="buscatch.jp", src=src)
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "buscatch.jp/rt3/toyama_map",
    }
