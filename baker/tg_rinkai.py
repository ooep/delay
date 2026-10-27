"""TG-Rinkai — TWR Rinkai Line live delay feed via the keyless Tokyo Glance mirror.

tokyoglance.com/lines/rinkai/ (lineId odpt.Railway:TWR.Rinkai) — keyless, no
  ODPT_KEY needed. Real objects in the 23:53 JST snapshot:
  odpt.TrainTimetable:TWR.Rinkai.2381T / .2370K (ShinagawaSeaside <-> Oimachi).
See baker/tokyoglance_common.py for the endpoint schema and the real-time proof.
Emits only trains with odpt:delay >= 60 s; late-night bodies are legitimately
empty (宁漏不误报).
"""

from . import tokyoglance_common

_RAILWAYS = {'odpt.Railway:TWR.Rinkai': 'りんかい線'}


def fetch(src=None):
    return tokyoglance_common.fetch(_RAILWAYS, src=src, operator="TG-Rinkai")
