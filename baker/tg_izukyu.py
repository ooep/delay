"""TG-Izukyu — Izu Kyuko Line live delay feed via the keyless Tokyo Glance mirror.

tokyoglance.com/lines/izukyu/ (lineId odpt.Railway:Izukyu.Izukyu).
See baker/tokyoglance_common.py for the endpoint schema and the real-time proof.
Emits only trains with odpt:delay >= 60 s; late-night bodies are legitimately
empty (宁漏不误报).
"""

from . import tokyoglance_common

_RAILWAYS = {'odpt.Railway:Izukyu.Izukyu': '伊豆急行線'}


def fetch(src=None):
    return tokyoglance_common.fetch(_RAILWAYS, src=src, operator="TG-Izukyu")
