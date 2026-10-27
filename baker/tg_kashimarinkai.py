"""鹿島臨海鉄道 — 鹿島臨海鉄道（大洗鹿島線） live delay feed via the keyless Tokyo Glance mirror.

Tokyo Glance mirrors ODPT TrainTimetable positions (odpt.Railway:KashimaRinkai.OaraiKashima)
and serves them keyless. See baker/tokyoglance_common.py for the endpoint
schema and real-time proof. Emits only trains with odpt:delay >= 60 s;
late-night bodies are legitimately empty (宁漏不误报).
"""

from . import tokyoglance_common

_RAILWAYS = { 'odpt.Railway:KashimaRinkai.OaraiKashima': '大洗鹿島線' }


def fetch(src=None):
    return tokyoglance_common.fetch(_RAILWAYS, src=src, operator="TG-KashimaRinkai")
