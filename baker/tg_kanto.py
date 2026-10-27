"""TG-Kanto — Kanto Railway (Joso + Ryugasaki) live delay feed via the keyless Tokyo Glance mirror.

tokyoglance.com/lines/joso/ + /lines/ryugasaki/ (embedded lineId). Real objects
  in the 2026-10-27 23:53 JST snapshot: odpt.TrainTimetable:KantoRailway.Joso.4184 /
  .1182 / .3185 / .4181 (legs Tobanoe->Daiho, Moriya->MinamiMoriya, ...). Parallel
  with the ODPT channel KantoRailway keyed entry in delays.py (needs ODPT_KEY); this
  keyless Tokyo Glance mirror covers both 常総線 and 竜ヶ崎線 with no key.
See baker/tokyoglance_common.py for the endpoint schema and the real-time proof.
Emits only trains with odpt:delay >= 60 s; late-night bodies are legitimately
empty (宁漏不误报).
"""

from . import tokyoglance_common

_RAILWAYS = {'odpt.Railway:KantoRailway.Joso': '常総線', 'odpt.Railway:KantoRailway.Ryugasaki': '竜ヶ崎線'}


def fetch(src=None):
    return tokyoglance_common.fetch(_RAILWAYS, src=src, operator="TG-Kanto")
