"""S37 (CEO 2026-10-21) - pyramiding study: the add waits for a completed
close at +kR with the stop at breakeven, is sized so combined open risk
stays within 1%, and carries its own stop at the original entry."""

import pandas as pd
import pytest

import study_exit_grid as g

ET = "America/New_York"
STEPS = [(1.0, 0.0), (1.5, 0.0), (2.0, 1.0)]
DAY = pd.Timestamp("2026-03-02", tz=ET)


def m5(prices, start="10:00"):
    idx = pd.date_range(f"2026-03-02 {start}", periods=len(prices), freq="5min", tz=ET)
    return pd.DataFrame([{"open": o, "high": h, "low": low, "close": c}
                         for o, h, low, c in prices], index=idx)


def sim(bars, k, target=106.0):
    # entry 100, stop 98 -> R = 2; target 106 = +3R
    return g.simulate_pyramid(bars, {}, {}, DAY, 100.0, 98.0, target, STEPS, k)


def test_no_add_is_the_live_single_unit_rule():
    bars = m5([(100, 100, 100, 100), (100, 102.5, 100, 102.2), (102, 106.5, 101.5, 106)])
    r = sim(bars, None)
    assert r["r"] == 3.0 and r["r_add"] is None and r["why"] == "target"


def test_add_at_1r_waits_for_the_close_and_buys_the_next_open():
    # bar 2 closes at 102.2 (+1.1R): ladder -> stop 100 (BE); add at bar 3 open 102.4
    bars = m5([(100, 100, 100, 100), (100, 102.5, 100, 102.2),
               (102.4, 106.5, 102.0, 106)])
    r = sim(bars, 1.0)
    assert r["add_px"] == 102.4
    # combined risk <= 1R: add risks 1R against its stop at 100 -> size 2/2.4
    assert r["size_add"] == pytest.approx(2 / 2.4)
    assert r["r_add"] == pytest.approx((2 / 2.4) * (106 - 102.4) / 2)
    assert r["r"] == pytest.approx(3.0 + r["r_add"])


def test_the_add_never_happens_before_the_stop_is_at_breakeven():
    # +1R touched intrabar but no close at +1R -> no BE stop -> no add
    bars = m5([(100, 100, 100, 100), (100, 102.5, 100.5, 101.5), (101.5, 101.6, 97.5, 98)])
    r = sim(bars, 1.0)
    assert r["add_px"] is None and r["r"] == -1.0


def test_an_add_stopped_at_its_own_stop_costs_one_original_r():
    bars = m5([(100, 100, 100, 100), (100, 102.5, 100.2, 102.2),
               (102.4, 102.5, 99.0, 99.5)])
    r = sim(bars, 1.0)
    assert r["why"] == "stop" and r["r_add"] == pytest.approx(-1.0)
    assert r["r"] == pytest.approx(0.0 - 1.0)       # first unit at BE


def test_add_at_1_5r_is_smaller_and_later():
    bars = m5([(100, 100, 100, 100), (100, 103.2, 100.5, 103.1),
               (103.2, 106.5, 103.0, 106)])
    r = sim(bars, 1.5)
    assert r["add_px"] == 103.2 and r["size_add"] == pytest.approx(2 / 3.2)


def test_rows_count_adds_dollars_and_the_cap():
    t = lambda r, add, risk, notional: {"setup": "momentum_continuation", "r": r,
                                         "r_add": add, "mfe_r": 1.5, "why": "x",
                                         "risk_usd": risk, "peak_notional": notional}
    res = {"no add (current)": [t(1.0, None, 50.0, 900.0)],
           "add at +1R": [t(2.0, 1.0, 100.0, 1800.0)]}
    rows = {(r["setup"], r["variant"]): r for r in g.s37_rows(res)}
    assert rows[("all", "add at +1R")]["risk_usd"] == 100.0
    assert rows[("all", "add at +1R")]["over_cap"] == 100.0
    assert rows[("all", "no add (current)")]["over_cap"] == 0.0
