"""TG-Shibayama — Shibayama Railway (芝山鉄道) live delay feed via the keyless Tokyo Glance mirror.

Tokyo Glance mirrors ODPT TrainTimetable positions (odpt.Railway:Shibayama.Shibayama)
and serves them keyless. Real object in the 2026-10-28 09:43 JST snapshot:
odpt.TrainTimetable:Shibayama.Shibayama.981.Weekday (E995-981). Note the envelope's
current-position field occasionally points at the shared Keisei NaritaSkyAccess
section, so only trains with odpt:delay >= 60 s are emitted — 宁漏不误报.
See baker/tokyoglance_common.py for the endpoint schema.
"""

from . import tokyoglance_common

_RAILWAYS = {'odpt.Railway:Shibayama.Shibayama': '芝山鉄道線'}


def fetch(src=None):
    return tokyoglance_common.fetch(_RAILWAYS, src=src, operator="TG-Shibayama")
