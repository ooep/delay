"""TG-Fujikyu — Fuji Kyuko Line live delay feed via the keyless Tokyo Glance mirror.

tokyoglance.com/lines/fujikyu/ (lineId odpt.Railway:Fujikyu.Fujikyu). Real object
  in the 23:53 JST snapshot: odpt.TrainTimetable:Fujikyu.Fujikyu.51 (Gekkoji ->
  MtFuji).
See baker/tokyoglance_common.py for the endpoint schema and the real-time proof.
Emits only trains with odpt:delay >= 60 s; late-night bodies are legitimately
empty (宁漏不误报).
"""

from . import tokyoglance_common

_RAILWAYS = {'odpt.Railway:Fujikyu.Fujikyu': '富士急行線'}


def fetch(src=None):
    return tokyoglance_common.fetch(_RAILWAYS, src=src, operator="TG-Fujikyu")
