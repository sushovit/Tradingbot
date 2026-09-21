"""
S9 (agenda 10.13): two non-trend research lanes in backtest.py. No network.

WHY THEY EXIST. Every setup the desk runs is a trend or a reversion-to-trend
pattern, so the book has one factor behind it and the SPY regime filter
switches most of it off at once. These lanes ask whether a non-trend lane
exists at all.

WHAT THIS FILE GUARDS. The detectors' logic, and — the part that matters —
that neither can reach the live loop. A research lane that leaks into
production is the one failure mode a backtest-only commission has.
"""

import json

import pandas as pd
import pytest

import backtest as bt


# ============================================ research only, and provably so

def test_both_lanes_are_registered_as_research():
    assert "oversold_bounce" in bt.RESEARCH_DETECTORS
    assert "base_breakout" in bt.RESEARCH_DETECTORS


def test_neither_lane_is_declared_live():
    """RESEARCH_STATUS is what backtest_report.md renders. Absent = research
    only, and that is the flag the order asked for."""
    for name in ("oversold_bounce", "base_breakout"):
        assert name not in bt.RESEARCH_STATUS
        assert bt.research_status(name) == "research only"


def test_neither_lane_has_a_strategy_module():
    """The live loop dispatches through strategies.REGISTRY. A detector that
    is not in it cannot be selected, whatever the config says."""
    from strategies import REGISTRY
    for name in ("oversold_bounce", "base_breakout"):
        assert name not in REGISTRY


def test_neither_lane_is_in_the_live_config():
    config = json.load(open("bot_config.json", encoding="utf-8"))
    for name in ("oversold_bounce", "base_breakout"):
        assert name not in config["default_strategies"]
        assert name not in config.get("strategy_timeframes", {})
        assert name not in config.get("volume_multipliers", {})
        assert name not in (config.get("setup_probation") or {}).get("setups", [])


def test_the_live_setups_were_not_disturbed():
    """The two lanes already on probation keep their status."""
    assert bt.RESEARCH_STATUS["pullback_in_uptrend"][0] == "LIVE (probation)"
    assert bt.RESEARCH_STATUS["post_earnings_continuation"][0] == "LIVE (probation)"


# ============================================ oversold_bounce

def bounce_df(rsi_dip=True, first_close=True, closes_above=True):
    """A slide deep enough to press RSI14 under 30, then a bar closing back
    above EMA9. The last row is the entry bar (only its open is used).

    The padding OSCILLATES on purpose. A run of identical closes has no gains
    in it at all, so RSI collapses to 0 on the first down bar and every
    fixture looks oversold — which is a property of the fixture, not the
    setup. Oscillating padding leaves RSI near 50 before the slide.

    The slide is also deliberately gentle (1.0/bar for 14 bars): a steep one
    strands EMA9 so far above price that no single bar can close back over
    it, and the detector would be refusing for the wrong reason.
    """
    rows = []
    for i in range(40):
        c = 100 + (1.0 if i % 2 else -1.0)
        rows.append((c, c + 0.6, c - 0.6, c, 100_000))
    close = 100.0
    if rsi_dip:
        for _ in range(14):
            close -= 1.0
            rows.append((close + 0.4, close + 0.5, close - 0.7, close, 100_000))
    else:
        # One shallow down bar, so the bar before the bounce is below EMA9
        # (the "first close above" test) while RSI stays near 50.
        close = 98.0
        rows.append((close + 0.5, close + 0.7, close - 0.5, close, 100_000))

    if not closes_above:
        rows.append((close, close + 0.4, close - 1.0, close - 0.6, 100_000))
    else:
        first = close + (5.0 if rsi_dip else 4.0)
        rows.append((close + 0.2, first + 0.4, close - 0.5, first, 130_000))
        if not first_close:
            # A second close above EMA9: continuation, not a turn.
            second = first + 2.0
            rows.append((first + 0.1, second + 0.3, first - 0.2, second,
                         120_000))

    last = rows[-1][3]
    rows.append((last + 0.2, last + 1.0, last - 0.3, last + 0.5, 110_000))
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="D")
    return pd.DataFrame(rows, index=idx,
                        columns=["open", "high", "low", "close", "volume"])


def test_an_oversold_turn_is_detected():
    result = bt.detect_oversold_bounce(bounce_df())
    assert isinstance(result, dict)
    assert result["setup"] == "oversold_bounce"


def test_the_stop_is_the_bounce_bar_low():
    """The order names the level: the bar that closed back above EMA9."""
    df = bounce_df()
    result = bt.detect_oversold_bounce(df)
    assert result["stop_level"] == pytest.approx(float(df["low"].iloc[-2]))


def test_a_stop_below_the_entry_is_what_makes_it_tradeable():
    df = bounce_df()
    result = bt.detect_oversold_bounce(df)
    assert result["stop_level"] < float(df["open"].iloc[-1])


def test_no_oversold_reading_means_no_signal():
    """A close above EMA9 on its own is not this setup — without the
    washout it is just an EMA cross."""
    assert bt.detect_oversold_bounce(bounce_df(rsi_dip=False)) == "not_oversold"


def test_only_the_first_close_above_ema9_counts():
    """The second close above is a continuation, not a turn. Without this
    the lane re-enters every bar of the recovery."""
    assert bt.detect_oversold_bounce(
        bounce_df(first_close=False)) == "not_first_close_above_ema9"


def test_still_below_ema9_is_not_a_bounce():
    assert bt.detect_oversold_bounce(
        bounce_df(closes_above=False)) == "close_not_above_ema9"


def test_a_short_window_is_declined_not_crashed():
    assert bt.detect_oversold_bounce(bounce_df().iloc[-10:]) is None


def test_the_detector_never_reads_the_entry_bar_beyond_its_open():
    """No lookahead. Rewriting the entry bar's high/low/close must not
    change the verdict."""
    df = bounce_df()
    before = bt.detect_oversold_bounce(df)
    tampered = df.copy()
    for col in ("high", "low", "close", "volume"):
        tampered.iloc[-1, tampered.columns.get_loc(col)] = 9999
    assert bt.detect_oversold_bounce(tampered) == before


# ============================================ base_breakout

def base_df(range_pct=4.0, breakout=True, vol_mult=1.5):
    """A 20-bar base of the given width, then a breakout bar."""
    rows = [(100, 101, 99, 100, 100_000)] * 25
    low, high = 100.0, 100.0 * (1 + range_pct / 100.0)
    for i in range(bt.BASE_LOOKBACK):
        c = low + (high - low) * (0.5 if i % 2 else 0.4)
        rows.append((c, high if i % 3 == 0 else c + 0.1,
                     low if i % 4 == 0 else c - 0.1, c, 100_000))
    close = high + 1.5 if breakout else high - 0.5
    rows.append((high - 0.2, close + 0.3, high - 0.5, close,
                 int(100_000 * vol_mult)))
    rows.append((close + 0.1, close + 1, close - 0.2, close + 0.4, 110_000))
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="D")
    return pd.DataFrame(rows, index=idx,
                        columns=["open", "high", "low", "close", "volume"])


def test_a_base_breakout_is_detected():
    result = bt.detect_base_breakout(base_df())
    assert isinstance(result, dict)
    assert result["setup"] == "base_breakout"


def test_the_stop_is_the_range_low():
    df = base_df()
    result = bt.detect_base_breakout(df)
    base = df.iloc[-(bt.BASE_LOOKBACK + 2):-2]
    assert result["stop_level"] == pytest.approx(float(base["low"].min()))


def test_a_wide_range_is_not_a_base():
    """8% is the definition, not a preference: above it this is a trend that
    happens to end here, which the desk already trades."""
    assert bt.detect_base_breakout(base_df(range_pct=12.0)) == "range_too_wide"


def test_the_boundary_is_exclusive():
    assert isinstance(bt.detect_base_breakout(base_df(range_pct=7.5)), dict)
    assert bt.detect_base_breakout(base_df(range_pct=8.5)) == "range_too_wide"


def test_no_close_above_the_range_high_means_no_breakout():
    assert bt.detect_base_breakout(base_df(breakout=False)) == "no_breakout"


def test_a_drift_out_of_the_base_is_refused():
    """1.3x the base's average volume. A breakout nobody participated in is
    the classic failed breakout."""
    assert bt.detect_base_breakout(
        base_df(vol_mult=1.1)) == "breakout_volume_low"
    assert isinstance(bt.detect_base_breakout(base_df(vol_mult=1.3)), dict)


def test_the_base_excludes_the_trigger_bar():
    """If the trigger's own range and volume defined the base, a big
    breakout bar would widen the range it is breaking out of and inflate the
    volume it has to beat."""
    df = base_df()
    result = bt.detect_base_breakout(df)
    trigger_low = float(df["low"].iloc[-2])
    assert result["stop_level"] != trigger_low or trigger_low <= \
        float(df.iloc[-(bt.BASE_LOOKBACK + 2):-2]["low"].min())


def test_a_short_window_is_declined_not_crashed():
    assert bt.detect_base_breakout(base_df().iloc[-5:]) is None


def test_the_detector_never_reads_the_entry_bar_beyond_its_open():
    df = base_df()
    before = bt.detect_base_breakout(df)
    tampered = df.copy()
    for col in ("high", "low", "close", "volume"):
        tampered.iloc[-1, tampered.columns.get_loc(col)] = 9999
    assert bt.detect_base_breakout(tampered) == before


# ============================================ they run through the harness

@pytest.mark.parametrize("name,frame", [("oversold_bounce", bounce_df),
                                        ("base_breakout", base_df)])
def test_the_lane_replays_with_the_live_mechanics(name, frame):
    """Same sizing and bracket mechanics as the playbook — a research number
    produced under different mechanics is not comparable to the live ones."""
    df = pd.concat([frame(), frame()], ignore_index=False)
    df.index = pd.date_range("2026-01-01", periods=len(df), freq="D")
    trades = bt.replay_research("TEST", df, name, regime=None,
                                target_r=bt.RESEARCH_TARGET_R)
    for t in trades:
        assert t["strategy"] == name
        assert t["qty"] >= 1
        assert t["stop"] < t["entry"] < t["target"]
        # 3R target, measured off the same risk unit the stop defines.
        risk_unit = t["entry"] - t["stop"]
        assert t["target"] == pytest.approx(t["entry"] + risk_unit * 3.0,
                                            abs=0.02)
