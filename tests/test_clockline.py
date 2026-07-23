"""
Dual-timezone self-dating headers — clockline.py. No network.

  - Nepal conversion across the date boundary (15:17 ET Thu -> 01:02 Fri)
  - the :45 offset exact (never hand-rolled)
  - session label at open/close/weekend/holiday edges
  - age annotation at print time
"""

from datetime import datetime, timedelta

import clockline
from clockline import ET, NEPAL


def et(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


def test_nepal_conversion_across_date_boundary():
    line = clockline.two_zone_line(et(2026, 7, 23, 15, 17))     # Thursday
    assert line.startswith("2026-07-23 15:17 ET  |  2026-07-24 01:02 Nepal")
    assert "US market: OPEN (closes in 0h43m)" in line          # spec example


def test_offset_is_exactly_9h45_in_summer():
    t = et(2026, 7, 23, 15, 17)
    diff = t.astimezone(NEPAL).utcoffset() - t.utcoffset()
    assert diff == timedelta(hours=9, minutes=45)


def test_offset_is_10h45_in_winter():
    # ET flips to EST (-5); Kathmandu never changes — offset becomes 10:45.
    t = et(2026, 12, 15, 12, 0)
    diff = t.astimezone(NEPAL).utcoffset() - t.utcoffset()
    assert diff == timedelta(hours=10, minutes=45)


def test_session_open_edges():
    assert "OPEN (closes in 6h30m)" in clockline.market_session_label(
        et(2026, 7, 23, 9, 30))                                  # first minute
    assert "OPEN (closes in 0h01m)" in clockline.market_session_label(
        et(2026, 7, 23, 15, 59))                                 # last minute
    label = clockline.market_session_label(et(2026, 7, 23, 16, 0))
    assert label.startswith("CLOSED (opens Fri 19:15 Nepal")     # at the bell


def test_session_premarket_and_weekend():
    # Thursday pre-open: opens TODAY 9:30 ET = 19:15 Nepal Thursday.
    assert clockline.market_session_label(
        et(2026, 7, 23, 9, 29)) == "CLOSED (opens Thu 19:15 Nepal)"
    # Saturday noon -> Monday.
    assert clockline.market_session_label(
        et(2026, 7, 25, 12, 0)) == "CLOSED (opens Mon 19:15 Nepal)"


def test_session_holiday_skipped():
    # Labor Day Mon 2026-09-07 is closed -> opens Tue. (September = EDT.)
    assert clockline.market_session_label(
        et(2026, 9, 7, 12, 0)) == "CLOSED (opens Tue 19:15 Nepal)"


def test_annotate_age_at_print_time():
    now = et(2026, 7, 23, 15, 47)
    text = ("# Paper Account Report\n"
            "2026-07-23 15:17 ET  |  2026-07-24 01:02 Nepal  |  US market: OPEN (closes in 0h43m)\n"
            "body line without stamp\n")
    out = clockline.annotate_age(text, now=now)
    assert "generated 30m ago" in out
    assert out.count("generated") == 1                # only the stamped line
    # Idempotent: annotating again adds nothing.
    assert clockline.annotate_age(out, now=now).count("generated") == 1


def test_annotate_age_hours_format():
    now = et(2026, 7, 23, 17, 2)
    text = "2026-07-23 15:17 ET  |  x\n"
    assert "generated 1h 45m ago" in clockline.annotate_age(text, now=now)
