"""Toyama Chihou Railway (富山地方鉄道) live feed: 鉄道線 (本線/立山線/
不二越・上滝線) + 市内電車, both served by the とやまロケーションシステム
buscatch rt3 backend. See baker/_buscatch.py for protocol notes.

Operator groups (same backend, two id_names):
  id_name="chitetsu_train"  rosen_group_id=2235  鉄道線
  id_name="chitetsu_tram"  rosen_group_id=2167 市内電車

市内電車 rosen labels are "A ⇒ B" pairs (the loop line has no 上り/下り
label), so _direction stays None there rather than being fabricated.
"""

from . import common
from . import _buscatch

# 鉄道線
TRAIN_GROUP = 2235
TRAIN_LINES = [
    {"keito_id": 1212, "rosen_id": 11971, "keito_name": "本線",
     "direction": "上り"},   # 上り(電鉄富山駅行き)
    {"keito_id": 1212, "rosen_id": 11972, "keito_name": "本線",
     "direction": "下り"},   # 下り(上市・電鉄黒部・宇奈月温泉行き)
    {"keito_id": 1213, "rosen_id": 11973, "keito_name": "立山線",
     "direction": "上り"},
    {"keito_id": 1213, "rosen_id": 11974, "keito_name": "立山線",
     "direction": "下り"},
    {"keito_id": 1214, "rosen_id": 11975, "keito_name": "不二越・上滝線",
     "direction": "上り"},
    {"keito_id": 1214, "rosen_id": 11976, "keito_name": "不二越・上滝線",
     "direction": "下り"},
]

# 市内電車
TRAM_GROUP = 2167
TRAM_LINES = [
    {"keito_id": 1030, "rosen_id": 8890, "keito_name": "市内電車",
     "direction": None},   # 南富山駅前 ⇒ 富山大学前
    {"keito_id": 1030, "rosen_id": 8891, "keito_name": "市内電車",
     "direction": None},   # 富山大学前 ⇒ 南富山駅前
    {"keito_id": 1031, "rosen_id": 8892, "keito_name": "市内電車",
     "direction": None},   # 岩瀬浜⇒環状線⇒岩瀬浜 (loop)
    {"keito_id": 1032, "rosen_id": 8893, "keito_name": "市内電車",
     "direction": None},   # 岩瀬浜 ⇒ 南富山駅前
    {"keito_id": 1032, "rosen_id": 8894, "keito_name": "市内電車",
     "direction": None},   # 南富山駅前 ⇒ 岩瀬浜
    {"keito_id": 1033, "rosen_id": 8895, "keito_name": "市内電車",
     "direction": None},   # 岩瀬浜 ⇒ 富山大学前
    {"keito_id": 1033, "rosen_id": 8896, "keito_name": "市内電車",
     "direction": None},   # 富山大学前 ⇒ 岩瀬浜
]


def fetch(src=None):
    src = src or common.Source()
    body = _buscatch.fetch_feed(
        "chitetsu_train", TRAIN_GROUP, TRAIN_LINES,
        railway_prefix="odpt.Railway:ToyamaChihou",
        source_domain="buscatch.jp", src=src)
    body += _buscatch.fetch_feed(
        "chitetsu_tram", TRAM_GROUP, TRAM_LINES,
        railway_prefix="odpt.Railway:ToyamaChihou",
        source_domain="buscatch.jp", src=src)
    return {
        "ok": True,
        "fetchedAt": common.jst_now().isoformat(),
        "body": body,
        "source": "buscatch.jp/rt3/toyama_map",
    }
