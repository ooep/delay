"""TG-Moka — Moka Railway live delay feed via the keyless Tokyo Glance mirror.

tokyoglance.com/lines/moka/ (lineId odpt.Railway:Moka.Moka).
See baker/tokyoglance_common.py for the endpoint schema and the real-time proof.
Emits only trains with odpt:delay >= 60 s; late-night bodies are legitimately
empty (宁漏不误报).
"""

from . import tokyoglance_common

_RAILWAYS = {'odpt.Railway:Moka.Moka': '真岡線'}


def fetch(src=None):
    return tokyoglance_common.fetch(_RAILWAYS, src=src, operator="TG-Moka")
