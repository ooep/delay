"""Japan national holidays (国民の祝日) + observed substitution days that JR
operators run their 祝日 (Saturday/Sunday/Holiday) timetable on.

Data source: 国立天文台 暦要項 国民の祝日
  https://eco.mtk.nao.ac.jp/koyomi/yoko/  (令和8/2026, 令和9/2027 発表)
The table below lists, for years officially published so far:
  - the 16 statutory shukujitsu, PLUS
  - the "休日になる" days (振替休日 when a shukujitsu falls on Sunday, and the
    sandwiched weekday in a 3-day weekend) - both are run on the holiday
    timetable by the JR group.

Unpublished years (2028+) deliberately fall back to plain weekday detection:
the vernal/autumnal equinox dates are astronomically computed and not yet
fixed, so guessing them would be less reliable than the weekday-only fallback.
Update this table every February when the observatory publishes the next
year's calendar.

ISO date strings ("YYYY-MM-DD"), O(1) membership test.
"""

PUBLISHED_HOLIDAYS = frozenset({
    # 2026 (令和8) - 16 shukujitsu + 5/6 振替・3連休措置 + 9/22 3連休措置
    "2026-01-01", "2026-01-12", "2026-02-11", "2026-02-23", "2026-03-20",
    "2026-04-29", "2026-05-03", "2026-05-04", "2026-05-05", "2026-05-06",
    "2026-07-20", "2026-08-11", "2026-09-21", "2026-09-22", "2026-09-23",
    "2026-10-12", "2026-11-03", "2026-11-23",
    # 2027 (令和9) - 16 shukujitsu + 3/22 振替 (春分 3/21 is Sunday)
    "2027-01-01", "2027-01-11", "2027-02-11", "2027-02-23", "2027-03-21",
    "2027-03-22",
    "2027-04-29", "2027-05-03", "2027-05-04", "2027-05-05",
    "2027-07-19", "2027-08-11", "2027-09-20", "2027-09-23",
    "2027-10-11", "2027-11-03", "2027-11-23",
})


def is_holiday_today(today_iso):
    """True when today_iso (YYYY-MM-DD, JST) is a day JR runs the 祝日
    timetable - a shukujitsu, its 振替, or a sandwiched 3-day-weekend day."""
    return today_iso in PUBLISHED_HOLIDAYS
