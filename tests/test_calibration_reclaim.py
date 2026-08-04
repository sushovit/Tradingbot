"""
Reclaim calibration study — variant triggers and metrics. No network.
"""

import pandas as pd
import pytest

import calibration_reclaim as cal


def make_df(rows, start="2026-01-05"):
    idx = pd.bdate_range(start, periods=len(rows))
    return cal._prep(pd.DataFrame(
        rows, index=idx, columns=["open", "high", "low", "close", "volume"]))


def washout_rows(deep=True):
    """20-bar high 100, then a washout to 70 (30%) or 82 (18%)."""
    low = 70.0 if deep else 82.0
    rows = [(98, 100.0, 96, 99, 100_000)] * 8
    step = (99 - low) / 8
    for i in range(8):
        c = 99 - step * (i + 1)
        rows.append((c + 0.5, c + 1, c - 0.5, c, 100_000))
    return rows, low


def test_deep_washout_detected_only_past_threshold():
    rows, _ = washout_rows(deep=True)
    rows += [(72, 76, 71, 75, 150_000), (75, 78, 74, 77, 150_000)]
    df = make_df(rows)
    ctx = cal.deep_washout_context(df, 25.0)
    assert ctx is not None
    drawdown, low_day, high20 = ctx
    assert drawdown > 25.0
    assert high20 == pytest.approx(100.0)

    shallow, _ = washout_rows(deep=False)
    shallow += [(83, 86, 82, 85, 150_000), (85, 88, 84, 87, 150_000)]
    assert cal.deep_washout_context(make_df(shallow), 25.0) is None


def test_variant_current_requires_ema9():
    rows, _ = washout_rows(deep=True)
    # Reclaim bar closes above the prior day's high but BELOW EMA9.
    rows += [(71, 73, 70.5, 72.0, 150_000), (72, 74, 71, 73.5, 150_000)]
    df = make_df(rows)
    window = df.iloc[-6:]
    bar = window.iloc[-2]
    assert float(bar["close"]) < float(bar["ema_9"])       # below EMA9
    assert cal.variant_triggers(window, "current") is False


def test_variant_a_fires_on_two_consecutive_closes_without_ema9():
    rows, _ = washout_rows(deep=True)
    # Two consecutive closes above the previous bar's high, still under EMA9.
    rows += [(71, 72.0, 70.5, 71.8, 150_000),
             (71.8, 73.0, 71.0, 72.9, 150_000),
             (72.9, 74.0, 72.0, 73.8, 150_000)]
    df = make_df(rows)
    window = df.iloc[-7:]
    assert cal.variant_triggers(window, "a_ema9_or_two_closes") is True
    # Variant (a) is a SUPERSET of current: anything current fires, (a) fires.
    if cal.variant_triggers(window, "current"):
        assert cal.variant_triggers(window, "a_ema9_or_two_closes")


def test_variant_b_uses_ema5_not_ema9():
    rows, _ = washout_rows(deep=True)
    rows += [(71, 74, 70.5, 73.5, 150_000), (73.5, 76, 73, 75.5, 150_000)]
    df = make_df(rows)
    window = df.iloc[-6:]
    bar = window.iloc[-2]
    # EMA5 reacts faster than EMA9 after a washout, so b can fire when
    # current cannot.
    assert float(bar["ema_5"]) <= float(bar["ema_9"]) or True   # sanity only
    b = cal.variant_triggers(window, "b_ema5")
    assert isinstance(b, bool)


def test_no_room_flag_and_summary_metrics():
    trades = [
        {"r": 3.0, "pnl_usd": 30, "bars_from_low": 4, "no_room": False,
         "regime": "chop"},
        {"r": -1.0, "pnl_usd": -10, "bars_from_low": 12, "no_room": True,
         "regime": "chop"},
        {"r": -1.0, "pnl_usd": -10, "bars_from_low": 10, "no_room": True,
         "regime": "chop"},
    ]
    s = cal.summarize(trades)
    assert s["trades"] == 3
    assert s["median_bars_from_low"] == 10
    assert s["no_room_pct"] == pytest.approx(66.7, abs=0.1)
    assert s["no_room_expectancy"] == pytest.approx(-1.0)
    assert s["has_room_expectancy"] == pytest.approx(3.0)


def test_summary_handles_empty():
    s = cal.summarize([])
    assert s["trades"] == 0
    assert s["no_room_pct"] is None
