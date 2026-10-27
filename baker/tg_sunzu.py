"""TG-Sunzu — Izuhakone Railway Sunzu Line live delay feed via the keyless Tokyo Glance mirror.

tokyoglance.com/lines/sunzu/ (lineId odpt.Railway:IzuHakone.Sunzu). Real objects
  in the 23:53 JST snapshot: IzuHakone.Sunzu.125 / .227. Only the Sunzu line is
  matched — IzuHakone.Daiyuzan (大雄山線) is deliberately excluded.
See baker/tokyoglance_common.py for the endpoint schema and the real-time proof.
Emits only trains with odpt:delay >= 60 s; late-night bodies are legitimately
empty (宁漏不误报).
"""

from . import tokyoglance_common

_RAILWAYS = {'odpt.Railway:IzuHakone.Sunzu': '駿豆線'}


def fetch(src=None):
    return tokyoglance_common.fetch(_RAILWAYS, src=src, operator="TG-Sunzu")
