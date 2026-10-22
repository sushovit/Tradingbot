"""S49 (CEO 2026-10-22): reclaim entry time (open / 09:45 / 10:00) and the
sector cap, replayed through the live daily rule. Synthetic data only."""

import pandas as pd

import study_exit_grid as g

ET = "America/New_York"


def day_bars(prices, start="09:30"):
    idx = pd.date_range(f"2026-03-02 {start}", periods=len(prices), freq="5min", tz=ET)
    return pd.DataFrame([{"open": o, "high": h, "low": l, "close": c}
                         for o, h, l, c in prices], index=idx)


def test_entry_at_the_open_is_the_first_bar():
    b = day_bars([(100, 101, 99, 100.5)] * 4)
    assert g.s49_delayed_entry(b, 95.0, None) == (100.0, b.index[0])


def test_a_delayed_entry_buys_the_open_of_the_first_bar_at_or_after_the_time():
    b = day_bars([(100, 101, 99, 100.5)] * 3 + [(101.2, 102, 101, 101.8)] * 4)  # 09:45 = 4th bar
    px, ts = g.s49_delayed_entry(b, 95.0, (9, 45))
    assert px == 101.2 and (ts.hour, ts.minute) == (9, 45)


def test_a_delayed_entry_is_avoided_if_the_stop_traded_first():
    b = day_bars([(100, 100.5, 94.5, 95)] + [(96, 97, 95.5, 96.5)] * 6)
    assert g.s49_delayed_entry(b, 95.0, (9, 45)) == (None, "avoided")


def test_exit_timestamp_is_returned():
    b = day_bars([(100, 100, 100, 100), (100, 100.2, 97.5, 98)], start="10:00")
    r, why, mfe, ts = g.simulate_with_exit(b, {}, {}, pd.Timestamp("2026-03-02", tz=ET),
                                          100.0, 98.0, 106.0, [(1.0, 0.0)])
    assert (r, why) == (-1.0, "stop") and ts == b.index[1]


def test_decision_needs_expectancy_at_least_current_and_smaller_drawdown():
    s = lambda e, dd: {"trades": 100, "expectancy_r": e, "max_drawdown_r": dd}
    assert g.s49_decision({None: s(0.10, 20), (9, 45): s(0.12, 15), (10, 0): s(0.15, 25)}) == (9, 45)
    assert g.s49_decision({None: s(0.10, 20), (9, 45): s(0.09, 10), (10, 0): s(0.15, 25)}) is None
    assert g.s49_decision({None: s(0.10, 20), (9, 45): s(0.11, 12), (10, 0): s(0.14, 11)}) == (10, 0)


def test_labels():
    assert g.s49_label(None) == "at the open (current)" and g.s49_label((10, 0)) == "not before 10:00"
