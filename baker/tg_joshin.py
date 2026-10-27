"""TG-Joshin — Joshin Electric Railway live delay feed via the keyless Tokyo Glance mirror.

tokyoglance.com/lines/joshin/ (lineId odpt.Railway:Joshin.Joshin). Live strings
  already reference odpt.Station:Joshin.Joshin.Takasaki on 2026-10-28.
See baker/tokyoglance_common.py for the endpoint schema and the real-time proof.
Emits only trains with odpt:delay >= 60 s; late-night bodies are legitimately
empty (宁漏不误报).
"""

from . import tokyoglance_common

_RAILWAYS = {'odpt.Railway:Joshin.Joshin': '上信線'}


def fetch(src=None):
    return tokyoglance_common.fetch(_RAILWAYS, src=src, operator="TG-Joshin")
