"""千葉都市モノレール — 千葉都市モノレール（1号線・2号線） live delay feed via the keyless Tokyo Glance mirror.

Tokyo Glance mirrors ODPT TrainTimetable positions (odpt.Railway:ChibaMonorail.Line1)
and serves them keyless. See baker/tokyoglance_common.py for the endpoint
schema and real-time proof. Emits only trains with odpt:delay >= 60 s;
late-night bodies are legitimately empty (宁漏不误报).
"""

from . import tokyoglance_common

_RAILWAYS = { 'odpt.Railway:ChibaMonorail.Line1': '千葉都市モノレール1号線', 'odpt.Railway:ChibaMonorail.Line2': '千葉都市モノレール2号線' }


def fetch(src=None):
    return tokyoglance_common.fetch(_RAILWAYS, src=src, operator="TG-ChibaMonorail")
