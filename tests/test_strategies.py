"""
Strategy verification tests — synthetic OHLCV fixtures, no network.

For each playbook strategy:
  - fires on a textbook example of its setup
  - does NOT fire on a counterexample (choppy/whipsaw data)
  - its stop is placed at the correct invalidation level
  - a signal with R:R < 1.5 is rejected downstream (risk.check_signal)
  - rejected signals appear in the journal as passes (source="rules")
"""

import sqlite3

import pandas as pd
import pandas_ta as ta
import pytest

import risk
from strategies import enabled_strategies
from strategies.base import Signal, Rejection
from strategies.trend_continuation import TrendContinuation
from strategies.momentum_continuation import MomentumContinuation
from strategies.mean_reversion_reclaim import MeanReversionReclaim


def make_df(rows):
    """rows: list of (open, high, low, close, volume)."""
    idx = pd.date_range("2026-06-01 09:30", periods=len(rows), freq="5min")
    return pd.DataFrame(rows, index=idx,
                        columns=["open", "high", "low", "close", "volume"])


RISK_PROFILE = {
    "fast_ema": 9, "slow_ema": 21, "adx_threshold": 10,
    "risk_per_trade_pct": 1.0, "rr_ratio": 2.0, "atr_multiplier": 2.0,
    "trailing_stop_type": "ATR", "trailing_stop_value": 2.0,
    "use_volume_filter": False,
}


def ctx(**config):
    return {"ticker": "TEST", "risk_profile": dict(RISK_PROFILE), "config": config}


# =============================================================================
# momentum_continuation
# =============================================================================

def momentum_textbook_df():
    rows = [(100, 101, 99, 100, 100_000)] * 28
    rows.append((100, 105.5, 100.2, 105.0, 200_000))   # breakout bar: +5%, 2x volume
    rows.append((105, 106, 104.8, 105.5, 120_000))     # next bar (entry bar)
    return make_df(rows)


def test_momentum_fires_on_textbook_breakout():
    signal = MomentumContinuation().detect(momentum_textbook_df(), ctx())
    assert isinstance(signal, Signal)
    assert signal.setup_name == "momentum_continuation"
    assert signal.entry == pytest.approx(105.5)


def test_momentum_stop_at_breakout_bar_low():
    signal = MomentumContinuation().detect(momentum_textbook_df(), ctx())
    assert isinstance(signal, Signal)
    assert signal.stop == pytest.approx(100.2)   # invalidation: breakout bar low
    # target floor is 3R (boardroom 2026-07-28, was 2R)
    assert signal.target == pytest.approx(
        signal.entry + 3 * (signal.entry - signal.stop))


def test_momentum_does_not_fire_on_chop():
    rows = []
    for i in range(30):  # whipsaw between 100 and 102, never above the range high
        c = 100 if i % 2 == 0 else 102
        rows.append((101, 103, 99, c, 100_000))
    result = MomentumContinuation().detect(make_df(rows), ctx())
    assert result is None


def test_momentum_low_volume_breakout_rejected():
    rows = [(100, 101, 99, 100, 100_000)] * 28
    rows.append((100, 105.5, 100.2, 105.0, 120_000))   # breakout but only 1.2x volume
    rows.append((105, 106, 104.8, 105.5, 120_000))
    result = MomentumContinuation().detect(make_df(rows), ctx())
    assert isinstance(result, Rejection)
    assert result.filter_name == "volume_low"


# =============================================================================
# mean_reversion_reclaim
# =============================================================================

def reclaim_textbook_df():
    rows = [(100, 100.5, 99, 100, 100_000)] * 10          # top of range: high ~100
    for i in range(13):                                   # washout: -15% to ~85
        c = 98 - i
        rows.append((c + 0.5, c + 1, c - 1, c, 100_000))  # lows reach 84
    rows.append((85, 86, 84.5, 85.5, 100_000))
    rows.append((85.5, 88.0, 85, 87, 100_000))            # prior bar: high 88
    rows.append((87, 90.5, 87.5, 90, 150_000))            # reclaim: close 90 > 88, 1.5x vol
    rows.append((90, 91, 89.5, 90.5, 110_000))            # current bar
    return make_df(rows)


def test_reclaim_fires_on_textbook_setup():
    signal = MeanReversionReclaim().detect(reclaim_textbook_df(), ctx())
    assert isinstance(signal, Signal)
    assert signal.setup_name == "mean_reversion_reclaim"


def test_reclaim_stop_below_reclaim_bar_low():
    signal = MeanReversionReclaim().detect(reclaim_textbook_df(), ctx())
    assert isinstance(signal, Signal)
    assert signal.stop == pytest.approx(87.5)   # invalidation: reclaim bar low


def test_reclaim_target_is_3r_floor_or_structural_high():
    signal = MeanReversionReclaim().detect(reclaim_textbook_df(), ctx())
    assert isinstance(signal, Signal)
    r = signal.entry - signal.stop
    floor_3r = signal.entry + 3 * r
    high_20 = signal.extras["high_20"]
    # Whichever is HIGHER: the 3R floor or the structural prior-high target.
    assert signal.target == pytest.approx(max(high_20, floor_3r))
    assert signal.target >= floor_3r - 1e-9


def test_reclaim_does_not_fire_without_washout():
    rows = [(100, 101, 99, 100, 100_000)] * 27            # flat — no 10% drawdown
    rows.append((100, 100.8, 99.5, 100.2, 100_000))
    rows.append((100.2, 101.5, 100, 101.2, 150_000))      # "reclaim" without washout
    rows.append((101, 102, 100.8, 101.5, 110_000))
    result = MeanReversionReclaim().detect(make_df(rows), ctx())
    assert result is None


def test_reclaim_low_volume_rejected():
    df = reclaim_textbook_df().copy()
    df.iloc[-2, df.columns.get_loc("volume")] = 105_000   # only 1.05x average
    result = MeanReversionReclaim().detect(df, ctx())
    assert isinstance(result, Rejection)
    assert result.filter_name == "volume_low"


# =============================================================================
# trend_continuation (moved golden-cross logic)
# =============================================================================

def trend_textbook_df():
    """Steady downtrend then sharp reversal; sliced so the EMA9/EMA21 golden
    cross lands exactly on the last CLOSED bar (iloc[-2])."""
    rows = []
    price = 150.0
    for _ in range(100):                    # downtrend keeps ADX elevated
        price -= 0.5
        rows.append((price + 0.3, price + 0.4, price - 0.5, price, 100_000))
    for _ in range(60):                     # sharp rally forces the cross
        price += 1.2
        rows.append((price - 0.8, price + 0.5, price - 1.0, price, 160_000))
    df = make_df(rows)
    ema9 = ta.ema(df["close"], length=9)
    ema21 = ta.ema(df["close"], length=21)
    cross_idx = None
    for i in range(101, len(df)):
        if ema9.iloc[i] > ema21.iloc[i] and ema9.iloc[i - 1] <= ema21.iloc[i - 1]:
            cross_idx = i
            break
    assert cross_idx is not None, "fixture failed to produce a golden cross"
    return df.iloc[:cross_idx + 2]          # cross bar becomes iloc[-2]


def test_trend_fires_on_textbook_golden_cross():
    signal = TrendContinuation().detect(trend_textbook_df(), ctx())
    assert isinstance(signal, Signal)
    assert signal.setup_name == "trend_continuation"


def test_trend_stop_is_atr_based_below_entry():
    signal = TrendContinuation().detect(trend_textbook_df(), ctx())
    assert isinstance(signal, Signal)
    assert 0 < signal.stop < signal.entry
    # target respects the profile's R multiple
    expected_target = signal.entry + (signal.entry - signal.stop) * RISK_PROFILE["rr_ratio"]
    assert signal.target == pytest.approx(expected_target)


def test_trend_does_not_fire_on_chop():
    rows = []
    for i in range(120):                    # whipsaw — EMAs interleave constantly
        c = 100 + (1.5 if i % 2 == 0 else -1.5)
        rows.append((100, 102.5, 97.5, c, 100_000))
    result = TrendContinuation().detect(make_df(rows), ctx())
    # No fresh cross on the last closed bar -> no signal considered.
    assert result is None or isinstance(result, Rejection)
    assert not isinstance(result, Signal)


def test_trend_adx_filter_rejects():
    context = ctx()
    context["risk_profile"]["adx_threshold"] = 99   # impossible bar
    result = TrendContinuation().detect(trend_textbook_df(), context)
    assert isinstance(result, Rejection)
    assert result.filter_name == "adx_low"


# =============================================================================
# Downstream pipeline: R:R gate + journal pass log
# =============================================================================

def test_low_rr_signal_rejected_downstream():
    # entry 100, stop 95, target 101 -> R:R = 0.2
    ok, reason = risk.check_signal(entry=100.0, stop=95.0, target=101.0,
                                   equity=1000.0)
    assert not ok
    assert "reward_risk" in reason


def test_rejected_signals_are_journaled_as_passes(temp_journal):
    df = momentum_textbook_df().copy()
    df.iloc[-2, df.columns.get_loc("volume")] = 120_000   # kill the volume filter
    result = MomentumContinuation().detect(df, ctx())
    assert isinstance(result, Rejection)

    # This is exactly what live_bot_worker does with a Rejection:
    temp_journal.log_rules_pass(result.ticker, result.setup_name,
                                result.filter_name, result.details)

    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM decisions").fetchall()
    conn.close()
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "rules"
    assert row["approved"] == 0
    assert "volume_low" in row["verdict"]


def test_per_ticker_strategy_config():
    config = {"strategies": {"NVDA": ["trend_continuation", "momentum_continuation"],
                             "AMD": ["mean_reversion_reclaim"]}}
    nvda = [s.name for s in enabled_strategies("NVDA", config)]
    amd = [s.name for s in enabled_strategies("AMD", config)]
    tsla = [s.name for s in enabled_strategies("TSLA", config)]  # not listed
    assert nvda == ["trend_continuation", "momentum_continuation"]
    assert amd == ["mean_reversion_reclaim"]
    assert tsla == ["trend_continuation"]   # default = original behaviour
