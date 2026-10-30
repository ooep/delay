"""横浜市営地下鉄 — Yokohama Municipal Subway live delay feed via the keyless Tokyo Glance mirror.

Tokyo Glance mirrors ODPT TrainTimetable positions; unlike the other TG lines,
the Yokohama Municipal objects carry an explicit `s` (delay seconds) even when
on time, so this feed can emit real delays. It fills the gap left by the
keyless ODPT mirror, which returns no YokohamaMunicipal trains without a key.

Blue Line = odpt.Railway:YokohamaMunicipal.Blue (1号線/3号線 at suki),
Green Line = odpt.Railway:YokohamaMunicipal.Green (4号線). See
baker/tokyoglance_common.py for the endpoint schema. Emits only trains with
odpt:delay >= 60 s; late-night bodies are legitimately empty (宁漏不误报).
"""

from . import tokyoglance_common

_RAILWAYS = {
    'odpt.Railway:YokohamaMunicipal.Blue': 'ブルーライン',
    'odpt.Railway:YokohamaMunicipal.Green': 'グリーンライン',
}


def fetch(src=None):
    return tokyoglance_common.fetch(_RAILWAYS, src=src, operator="TG-YokohamaMunicipal")
