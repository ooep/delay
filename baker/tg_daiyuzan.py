"""大雄山線 — Izuhakone Railway Daiyuzan Line live delay feed via the keyless Tokyo Glance mirror.

Tokyo Glance mirrors ODPT TrainTimetable positions (odpt.Railway:IzuHakone.Daiyuzan)
and serves them keyless. Kept separate from tg_sunzu.py so each line reports
under its own operator key. See baker/tokyoglance_common.py for the endpoint
schema and real-time proof. Emits only trains with odpt:delay >= 60 s;
late-night bodies are legitimately empty (宁漏不误报).
"""

from . import tokyoglance_common

_RAILWAYS = {'odpt.Railway:IzuHakone.Daiyuzan': '大雄山線'}


def fetch(src=None):
    return tokyoglance_common.fetch(_RAILWAYS, src=src, operator="TG-Daiyuzan")
