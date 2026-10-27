"""TG-HakoneTozan — Odakyu Hakone Tozan Railway live delay feed via the keyless Tokyo Glance mirror.

tokyoglance.com/lines/hakone-tozan/ (lineId odpt.Railway:OdakyuHakone.HakoneTozan).
  Matches only the Hakone-Tozan climbing section; the Odakyu main-line app data is
  a separate source and is not mixed in here.
See baker/tokyoglance_common.py for the endpoint schema and the real-time proof.
Emits only trains with odpt:delay >= 60 s; late-night bodies are legitimately
empty (宁漏不误报).
"""

from . import tokyoglance_common

_RAILWAYS = {'odpt.Railway:OdakyuHakone.HakoneTozan': '箱根登山線'}


def fetch(src=None):
    return tokyoglance_common.fetch(_RAILWAYS, src=src, operator="TG-HakoneTozan")
