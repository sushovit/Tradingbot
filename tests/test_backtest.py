"""
Goal 14 — backtest suite. Synthetic fixtures, no network.

  - no-lookahead guard: entry fills at the NEXT bar's OPEN, never its close;
    the signal bar itself is fully completed before the fill
  - fill arithmetic: stop/target/gap-through cases
  - a tiny synthetic end-to-end run producing one known trade
"""

import pandas as pd
import pytest

import backtest


def make_df(rows, start="2026-01-05"):
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame(rows, index=idx,
                        columns=["open", "high", "low", "close", "volume"])


# ---------------------------------------------------------------- fills

def test_fill_stop_hit():
    df = make_df([(100, 101, 99, 100, 1e6),
                  (100, 102, 94.9, 96, 1e6)])
    exit_price, exit_i, reason = backtest.simulate_bracket(
        df, 0, entry=100.0, stop=95.0, target=110.0)
    assert (exit_price, exit_i, reason) == (95.0, 1, "stop")


def test_fill_target_hit():
    df = make_df([(100, 101, 99, 100, 1e6),
                  (105, 111, 104, 110, 1e6)])
    exit_price, exit_i, reason = backtest.simulate_bracket(
        df, 0, entry=100.0, stop=95.0, target=110.0)
    assert (exit_price, exit_i, reason) == (110.0, 1, "target")


def test_gap_through_stop_fills_at_open_not_level():
    df = make_df([(100, 101, 99, 100, 1e6),
                  (90, 92, 88, 91, 1e6)])       # opens 5 below the stop
    exit_price, exit_i, reason = backtest.simulate_bracket(
        df, 0, entry=100.0, stop=95.0, target=110.0)
    assert (exit_price, reason) == (90.0, "gap_stop")   # real-world fill


def test_same_bar_both_levels_stop_first():
    df = make_df([(100, 111, 94, 100, 1e6)])   # spans stop AND target
    exit_price, _, reason = backtest.simulate_bracket(
        df, 0, entry=100.0, stop=95.0, target=110.0)
    assert (exit_price, reason) == (95.0, "stop")       # conservative


def test_never_exits_reports_open():
    df = make_df([(100, 101, 99, 100, 1e6)] * 5)
    exit_price, exit_i, reason = backtest.simulate_bracket(
        df, 0, entry=100.0, stop=90.0, target=120.0)
    assert exit_price is None and reason == "open"


# ---------------------------------------------------------------- no lookahead

def momentum_fixture(entry_open=104.0, entry_close=120.0):
    """Breakout on the signal bar; the ENTRY bar has a wildly different
    close so any close-based fill is unmistakable."""
    rows = [(100, 101, 99, 100, 100_000)] * (backtest.WARMUP_BARS - 1)
    rows.append((100, 105.5, 100.2, 105.0, 200_000))       # signal bar
    rows.append((entry_open, max(entry_open, entry_close) + 1,
                 min(entry_open, entry_close) - 6.0,
                 entry_close, 150_000))                     # entry bar
    rows += [(entry_close, entry_close + 1, entry_close - 1,
              entry_close, 150_000)] * 3
    return make_df(rows)


def test_entry_fills_at_next_bar_open_not_close():
    df = momentum_fixture(entry_open=104.0, entry_close=120.0)
    trades, _ = backtest.replay_symbol(
        "TEST", df, "momentum_continuation", None,
        backtest.BASE_RISK_PROFILE, backtest.BASE_CONFIG, target_r=2.0)
    assert trades, "expected one trade"
    assert trades[0]["entry"] == pytest.approx(104.0)   # OPEN, never 120
    # stop is the signal bar's low — known before the entry bar existed
    assert trades[0]["stop"] == pytest.approx(100.2)


def test_signal_bar_is_completed_before_entry():
    # entry date must be strictly AFTER the signal date
    df = momentum_fixture()
    trades, _ = backtest.replay_symbol(
        "TEST", df, "momentum_continuation", None,
        backtest.BASE_RISK_PROFILE, backtest.BASE_CONFIG, target_r=2.0)
    assert trades[0]["entry_date"] > trades[0]["signal_date"]


# ---------------------------------------------------------------- end to end

def test_synthetic_end_to_end_known_r():
    # Entry 104 (open), stop 100.2 (signal-bar low) -> risk 3.8/share.
    # Target at 2R = 111.6; make a later bar tag it exactly.
    rows = [(100, 101, 99, 100, 100_000)] * (backtest.WARMUP_BARS - 1)
    rows.append((100, 105.5, 100.2, 105.0, 200_000))       # signal bar
    rows.append((104, 105, 103, 104.5, 150_000))           # entry bar
    rows.append((105, 112, 104.5, 111, 150_000))           # tags 111.6
    df = make_df(rows)
    trades, _ = backtest.replay_symbol(
        "TEST", df, "momentum_continuation", None,
        backtest.BASE_RISK_PROFILE, backtest.BASE_CONFIG, target_r=2.0)
    assert len(trades) == 1
    t = trades[0]
    assert t["exit_reason"] == "target"
    assert t["r"] == pytest.approx(2.0, abs=0.01)
    assert t["qty"] >= 1
    stats = backtest.aggregate(trades)
    assert stats["trades"] == 1
    assert stats["win_rate"] == 100.0
    assert stats["expectancy_r"] == pytest.approx(2.0, abs=0.01)
