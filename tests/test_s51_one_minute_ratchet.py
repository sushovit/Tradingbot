"""S51 (CEO 2026-10-23): the R/ATR ladder's ratchet decided on 1-minute
closes vs 5-minute closes (current). Synthetic bars only - no network."""

from datetime import date

import pandas as pd

import study_exit_grid as g

STEPS = [(1.0, 0.0), (1.5, 0.0), (2.0, 1.0)]


def frame(rows):
    """rows: (ET 'YYYY-MM-DD HH:MM', open, high, low, close)."""
    idx = pd.DatetimeIndex([pd.Timestamp(r[0]).tz_localize(g.ET) for r in rows])
    return pd.DataFrame({"open": [r[1] for r in rows], "high": [r[2] for r in rows],
                         "low": [r[3] for r in rows], "close": [r[4] for r in rows],
                         "volume": 100}, index=idx)


def day_bars(day, start_min, end_min, px):
    """Flat 1-minute bars at `px` from start_min to end_min (minutes of day)."""
    return [(f"{day} {m // 60:02d}:{m % 60:02d}", px, px, px, px)
            for m in range(start_min, end_min)]


def sim(m1, mode, entry=100.0, stop=98.0, target=106.0, closes=None):
    return g.simulate_m1(m1, closes or {}, {}, pd.Timestamp(m1.index[0]), entry,
                         stop, target, STEPS, mode)


def test_windows_merge_overlapping_holds():
    w = g.s51_windows([date(2026, 1, 5), date(2026, 1, 12), date(2026, 3, 2)], hold=15)
    assert w == [(date(2026, 1, 5), date(2026, 1, 27)), (date(2026, 3, 2), date(2026, 3, 17))]
    assert g.s51_windows([]) == []


def test_qualifying_closes_per_mode():
    # 9:58, 9:59, 10:00, 10:02, 10:04, 10:05, 10:07 then next day 10:01
    mins = [598, 599, 600, 602, 604, 605, 607, 601]
    days = [1] * 7 + [2]
    assert g.s51_qualifying(mins, days, "1m") == [False, False, True, True, True, True, True, True]
    # 5m: the last PRINTED minute of each bucket >= 10:00 (sparse IEX minutes)
    assert g.s51_qualifying(mins, days, "5m") == [False, False, False, False, True, False, True, True]


def test_one_minute_ratchet_locks_a_spike_the_5m_bar_never_sees():
    d = "2026-03-02"
    rows = day_bars(d, 570, 600, 100.0)
    rows += [(f"{d} 10:00", 100.0, 104.2, 100.0, 104.0),   # +2R 1m close
             (f"{d} 10:01", 104.0, 104.0, 101.0, 101.5),   # back through entry + 1R
             (f"{d} 10:02", 101.5, 101.5, 100.5, 100.5),
             (f"{d} 10:03", 100.5, 100.5, 100.5, 100.5),
             (f"{d} 10:04", 100.5, 100.6, 100.4, 100.5)]   # 5m close 100.5 (< +1R)
    rows += day_bars(d, 605, 700, 100.5)
    rows += [(f"{d} 11:40", 100.5, 100.5, 97.0, 97.5)]      # original stop
    m1 = frame(rows)
    r1, why1, mfe1, _ = sim(m1, "1m")
    r5, why5, mfe5, _ = sim(m1, "5m")
    assert (r1, why1) == (1.0, "stop")        # stepped to entry + 1R at 10:00
    assert (r5, why5) == (-1.0, "stop")       # the 5m close never reached +1R
    assert abs(mfe1 - 2.1) < 1e-9 and abs(mfe5 - 2.1) < 1e-9


def test_same_path_on_completed_5m_closes_gives_the_same_result():
    d = "2026-03-02"
    rows = day_bars(d, 570, 600, 100.0)
    rows += day_bars(d, 600, 605, 102.5)                   # 5m and 1m close +1.25R
    rows += day_bars(d, 605, 610, 101.0)
    rows += [(f"{d} 10:10", 101.0, 101.0, 99.5, 99.8)]       # below entry
    m1 = frame(rows)
    a, b = sim(m1, "1m"), sim(m1, "5m")
    assert a[:2] == b[:2] == (0.0, "stop")                  # both at breakeven


def test_no_step_before_ten():
    d = "2026-03-02"
    rows = [(f"{d} 09:30", 100.0, 100.0, 100.0, 100.0),
            (f"{d} 09:31", 100.0, 105.0, 100.0, 104.9),       # +2.45R close at 9:31
            (f"{d} 09:32", 104.9, 104.9, 97.5, 97.6)]
    m1 = frame(rows)
    assert sim(m1, "1m")[:2] == (-1.0, "stop")


def test_target_and_gap_checked_on_every_minute_in_both_modes():
    d = "2026-03-02"
    rows = day_bars(d, 570, 575, 100.0) + [(f"{d} 09:35", 100.0, 106.5, 100.0, 106.0)]
    m1 = frame(rows)
    assert sim(m1, "1m")[:2] == sim(m1, "5m")[:2] == (3.0, "target")
    rows = day_bars(d, 570, 575, 100.0) + [(f"{d} 09:35", 97.0, 97.0, 96.0, 96.5)]
    m1 = frame(rows)
    assert sim(m1, "1m")[:2] == (-1.5, "stop (gap)")


def test_time_stop_day_five_at_1555_or_last_bar():
    days = ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"]
    rows = []
    for d in days:
        rows += day_bars(d, 570, 960, 100.2)
    r, why, mfe, ts = sim(frame(rows), "1m")
    assert why == "time stop day 5" and (ts.hour, ts.minute) == (15, 55)
    assert abs(r - 0.1) < 1e-9
    rows = []
    for d in days:
        rows += day_bars(d, 570, 900, 100.2)                 # nothing after 15:00
    rows += day_bars("2026-03-09", 570, 580, 100.2)
    r, why, _, ts = sim(frame(rows), "5m")
    assert why == "time stop day 5 (last bar)" and (ts.hour, ts.minute) == (14, 59)


def test_open_at_data_end_is_none():
    m1 = frame(day_bars("2026-03-02", 570, 600, 100.5))
    assert sim(m1, "1m") is None


def test_stats_and_rule():
    t = lambda r, mfe, d: {"r": r, "mfe_r": mfe, "day": d, "sym": "X"}
    cur = g.s51_stats([t(1.0, 2.0, 1), t(-1.0, 1.2, 2)])
    assert cur["total_r"] == 0.0 and cur["giveback"] == 50.0
    assert abs(cur["capture"] - 0.0) < 1e-9
    new = g.s51_stats([t(1.5, 2.0, 1), t(0.0, 1.2, 2)])
    assert new["capture"] > cur["capture"] and new["giveback"] == 0.0
    assert g.s51_decision(cur, new) is True
    worse = g.s51_stats([t(0.5, 2.0, 1), t(-1.0, 1.2, 2)])
    assert g.s51_decision(cur, worse) is False
    assert g.s51_decision(cur, g.s51_stats([])) is False


def test_diffs_pair_the_same_signal():
    a = [{"day": 1, "sym": "X", "setup": "s", "r": -1.0},
         {"day": 2, "sym": "Y", "setup": "s", "r": 0.5}]
    b = [{"day": 2, "sym": "Y", "setup": "s", "r": 0.5},
         {"day": 1, "sym": "X", "setup": "s", "r": 0.0}]
    pairs = g.s51_diffs(a, b)
    assert len(pairs) == 1 and pairs[0][0]["sym"] == "X" and pairs[0][1]["r"] == 0.0
