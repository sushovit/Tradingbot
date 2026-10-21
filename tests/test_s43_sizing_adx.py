"""CEO 2026-10-22: (1) every sizing budget follows the live config - no old
$1-2k cap anywhere; (3) the ADX gate passes at >= 30 and says so with two
decimals ('29.99 < 30', never '30.0 < 30')."""

import json

import numpy as np
import pandas as pd
import pytest

import backtest
import risk

CFG = json.load(open("bot_config.json", encoding="utf-8"))


def test_backtest_sizes_like_the_live_desk():
    assert backtest.BASE_EQUITY == float(CFG["capital_cap_usd"]) == 5000.0
    assert backtest.RISK_PCT == CFG["risk_profiles"]["Moderate"]["risk_per_trade_pct"]
    assert backtest.POSITION_CAP == risk.max_position_pct(CFG) == 0.25


def test_spot_10_08_is_affordable_on_the_current_config():
    """The one size_zero of October (SPOT 516.35 / 485.88, old $1,974 account):
    $49.98 risk budget buys 1 share at $30.47 risk; $1,249 notional cap fits."""
    eq = risk.effective_equity(4997.87, CFG)
    assert risk.position_size(eq, 1.0, 516.35, 485.88,
                              position_cap_pct=risk.max_position_pct(CFG)) == 1
    assert risk.position_size(backtest.BASE_EQUITY, backtest.RISK_PCT, 516.35, 485.88,
                              position_cap_pct=backtest.POSITION_CAP) == 1


def test_size_zero_report_counts_only_the_5k_account(temp_journal, monkeypatch):
    j = temp_journal
    j.log_rules_pass("SPOT", "mean_reversion_reclaim", "size_zero",
                     "entry=516.35 stop=485.88 stop_distance_usd=30.47 risk_budget_usd=19.75")
    import sqlite3
    conn = sqlite3.connect(j.DB_FILE)
    conn.execute("UPDATE decisions SET timestamp='2026-10-08 09:35:18'")
    conn.commit()
    assert j.size_zero_report("2026-10") == []                  # old account
    assert j.size_zero_report("2026-10", since="")[0]["size_zero"] == 1


def golden_cross_df():
    """Bars cut so the EMA9/EMA21 golden cross is on the last CLOSED bar."""
    import pandas_ta as ta
    close = np.r_[np.linspace(110, 100, 40), np.linspace(100.5, 112, 25)].astype(float)
    idx = pd.date_range("2026-10-01 09:30", periods=len(close), freq="5min")
    df = pd.DataFrame({"open": close, "high": close + 0.3, "low": close - 0.3,
                       "close": close, "volume": 10_000.0}, index=idx)
    f, s = ta.ema(df["close"], length=9), ta.ema(df["close"], length=21)
    k = next(i for i in range(22, len(df)) if f.iloc[i] > s.iloc[i] and f.iloc[i - 1] <= s.iloc[i - 1])
    return df.iloc[:k + 2]                      # cross at k = iloc[-2]


@pytest.mark.parametrize("adx,rejected", [(29.9999, True), (29.995, True), (30.0, False),
                                         (30.004, False)])
def test_adx_gate_passes_at_30_and_shows_two_decimals(monkeypatch, adx, rejected):
    import strategies.trend_continuation as tc
    from strategies.base import Rejection
    df = golden_cross_df()
    monkeypatch.setattr(tc.ta, "adx", lambda h, l, c, length=14: pd.DataFrame(
        {"ADX_14": [adx] * len(c)}, index=c.index))
    ctx = {"ticker": "X", "config": {"use_rsi_filter": False,
                                     "use_confirmation_candle": False},
           "risk_profile": {"fast_ema": 9, "slow_ema": 21, "adx_threshold": 30,
                            "rr_ratio": 3.0, "atr_multiplier": 2.0,
                            "use_volume_filter": False}}
    out = tc.TrendContinuation().detect(df, ctx)
    assert out is not None, "fixture must produce a golden cross"
    is_adx_reject = isinstance(out, Rejection) and out.filter_name == "adx_low"
    assert is_adx_reject is rejected
    if rejected:
        assert out.details == "ADX 29.99 < 30"            # rounded down, never 30.00
        assert "30.0" not in out.details
