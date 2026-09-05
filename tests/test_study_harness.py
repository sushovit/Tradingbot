"""
W3 (PM_PLAN.md): the study harness. No network — bars are hand-built.

The studies are only worth what the harness under them is worth, and two
pieces here are genuinely new logic rather than reporting:

  simulate_exit  — the trailing simulator W3(c) needed. backtest.py models a
                   STATIC stop and never trails, so neither exit rule had
                   ever been simulated in this repository.
  adx_at         — ADX is Wilder-smoothed and window-dependent. Computed
                   over the full series instead of the detector's 60-bar
                   window it left 35 of 62 trades outside both study bands.
"""

import pandas as pd
import pytest

import backtest
import study_common as sc


def frame(rows, start="2026-01-01"):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close",
                                       "volume"],
                        index=pd.bdate_range(start, periods=len(rows)))


def run_up_then_collapse(spread=6.0):
    """Entry 100, stop 90 (1R = 10). Runs to 111 (+1.1R), then gives it all
    back. Wide bars so the ATR trail sits BELOW entry — the case the
    breakeven floor exists for."""
    rows = [(100, 100, 100, 100, 1000)]
    for c in [100, 105, 111, 108, 104, 99, 95, 92, 88]:
        rows.append((c, c + spread, c - spread, c, 1000))
    return frame(rows)


# ============================================================ the three modes

def test_static_mode_reproduces_the_backtest_simulator():
    """The control must match backtest.simulate_bracket exactly, or the
    comparison measures the simulator rather than the exit rule."""
    df = run_up_then_collapse()
    mine = sc.simulate_exit(df, 1, 100.0, 90.0, 140.0, mode="static")
    theirs = backtest.simulate_bracket(df, 1, 100.0, 90.0, 140.0)
    assert mine == theirs


def test_the_floor_turns_a_give_back_into_breakeven():
    df = run_up_then_collapse()
    atr = sc.atr_series(df, length=3)
    static = sc.simulate_exit(df, 1, 100.0, 90.0, 140.0, mode="static")
    trail = sc.simulate_exit(df, 1, 100.0, 90.0, 140.0, mode="atr",
                             atr=atr, atr_mult=2.0)
    floor = sc.simulate_exit(df, 1, 100.0, 90.0, 140.0, mode="floor",
                             atr=atr, atr_mult=2.0)
    assert static[0] == pytest.approx(90.0)      # -1.0R
    assert 90.0 < trail[0] < 100.0               # helped, still a loss
    assert floor[0] == pytest.approx(100.0)      # breakeven, by construction
    assert floor[1] <= trail[1]                  # and it exits no later


def test_no_trailing_happens_before_plus_1r():
    """The structural stop must stand until the trade pays for its own risk —
    the NOK protection, in simulation."""
    rows = [(100, 100, 100, 100, 1000)]
    for c in [100, 102, 104, 103, 95, 89]:       # peaks at +0.4R, then dies
        rows.append((c, c + 1, c - 1, c, 1000))
    df = frame(rows)
    atr = sc.atr_series(df, length=3)
    outcomes = set()
    for mode in ("static", "atr", "floor"):
        price, _, reason = sc.simulate_exit(df, 1, 100.0, 90.0, 140.0,
                                            mode=mode, atr=atr, atr_mult=2.0)
        assert reason in ("stop", "gap_stop"), mode
        # The stop never moved: an exit at or below the ORIGINAL 90 proves it.
        assert price <= 90.0, mode
        outcomes.add((price, reason))
    assert len(outcomes) == 1, "all three modes must agree below +1R"


def test_the_stop_only_ever_ratchets_upward():
    """A widening ATR must not walk the stop back down."""
    rows = [(100, 100, 100, 100, 1000)]
    for c, s in [(100, 1), (112, 1), (113, 1), (113, 20), (113, 20)]:
        rows.append((c, c + s, c - s, c, 1000))
    df = frame(rows)
    atr = sc.atr_series(df, length=3)
    price, _, reason = sc.simulate_exit(df, 1, 100.0, 90.0, 400.0,
                                        mode="floor", atr=atr, atr_mult=2.0)
    # Whatever happens, it can never exit below the breakeven floor.
    assert price is None or price >= 100.0


def test_the_floor_is_never_set_above_the_bar_that_sets_it():
    """A stop at or above the close would be an instant fill, which is not
    what the live broker would do."""
    df = run_up_then_collapse()
    atr = sc.atr_series(df, length=3)
    price, _, _ = sc.simulate_exit(df, 1, 100.0, 90.0, 140.0, mode="floor",
                                   atr=atr, atr_mult=2.0)
    assert price is not None and price <= 111.0


def test_a_clean_winner_reaches_target_under_every_mode():
    rows = [(100, 100, 100, 100, 1000)]
    for c in [100, 106, 112, 118, 124, 131, 140]:
        rows.append((c, c + 1, c - 1, c, 1000))
    df = frame(rows)
    atr = sc.atr_series(df, length=3)
    for mode in ("static", "atr", "floor"):
        _, _, reason = sc.simulate_exit(df, 1, 100.0, 90.0, 130.0, mode=mode,
                                        atr=atr, atr_mult=2.0)
        assert reason in ("target", "gap_target"), mode


# ============================================================ features

def test_adx_uses_the_detectors_window_not_the_full_series():
    """Wilder smoothing is recursive: the same bar scores differently over 60
    bars than over 750. The study must bucket on the number the DETECTOR
    saw."""
    import pandas_ta as ta
    df = backtest.load_daily("NVDA", years=3)
    if df is None or len(df) < 300:
        pytest.skip("NVDA daily cache unavailable")
    full = ta.adx(df["high"], df["low"], df["close"], length=14)
    col = [c for c in full.columns if c.startswith("ADX")][0]
    pos = 400
    windowed = sc.adx_at(df, pos)
    assert windowed is not None
    # They must actually differ, or the fix was pointless.
    assert abs(windowed - float(full[col].iloc[pos])) > 0.5


def test_volume_ratio_is_the_signal_bar_over_its_trailing_average():
    rows = [(10, 10, 10, 10, 100)] * 20 + [(10, 10, 10, 10, 300)]
    df = frame(rows)
    assert sc.volume_ratio_at(df, 20) == pytest.approx(3.0)
    assert sc.volume_ratio_at(df, 2) is None          # not enough history


# ============================================================ isolation

def test_study_profile_and_config_never_mutate_the_backtest_defaults():
    """Studies lower thresholds to widen the population; that must not leak
    into the shared defaults or every later study is wrong."""
    before_profile = dict(backtest.BASE_RISK_PROFILE)
    before_config = dict(backtest.BASE_CONFIG)
    p = sc.study_profile(adx_threshold=25)
    c = sc.study_config(volume_multipliers={"momentum_continuation": 1.0})
    p["adx_threshold"] = 1
    c["volume_multipliers"]["momentum_continuation"] = 9
    assert backtest.BASE_RISK_PROFILE == before_profile
    assert backtest.BASE_CONFIG == before_config


def test_aggregate_reports_nothing_rather_than_zero_on_an_empty_bucket():
    stats = sc.aggregate([])
    assert stats["trades"] == 0
    assert stats["expectancy_r"] is None          # not 0.0, which reads as flat
    assert sc.fmt(stats)[1] == "—"
