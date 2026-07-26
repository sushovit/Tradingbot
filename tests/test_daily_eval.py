"""
Goal 15 — daily-bar timeframe migration. No network.

  - daily strategies evaluate once per completed bar, re-arm on a new bar
  - Rule #3 gap-abort auto-populates from the signal bar midpoint
  - trend_continuation keeps its intraday timeframe (5-min path unchanged)
"""

import pandas as pd

import daily_eval


def daily_df(days, include_today=False, today="2026-07-24"):
    n = days + (1 if include_today else 0)
    end = pd.Timestamp(today) if include_today \
        else pd.Timestamp(today) - pd.tseries.offsets.BDay(1)
    idx = pd.bdate_range(end=end, periods=n)
    rows = [(100.0, 102.0, 98.0, 101.0, 1e6)] * n
    return pd.DataFrame(rows, index=idx,
                        columns=["open", "high", "low", "close", "volume"])


def test_completed_bar_excludes_todays_partial():
    df = daily_df(5, include_today=True)
    assert daily_eval.completed_bar_date(df, "2026-07-24") == "2026-07-23"
    df2 = daily_df(5, include_today=False)
    assert daily_eval.completed_bar_date(df2, "2026-07-24") == "2026-07-23"


def test_daily_eval_fires_once_per_bar():
    evaluated = {}
    df = daily_df(5, include_today=True)
    assert daily_eval.should_evaluate(evaluated, "NVDA", "mean_reversion_reclaim",
                                      df, "2026-07-24") is True
    daily_eval.mark_evaluated(evaluated, "NVDA", "mean_reversion_reclaim",
                              df, "2026-07-24")
    # Same completed bar -> no second evaluation, ever.
    for _ in range(5):
        assert daily_eval.should_evaluate(evaluated, "NVDA",
                                          "mean_reversion_reclaim",
                                          df, "2026-07-24") is False
    # A NEW completed bar re-arms it.
    df_next = daily_df(6, include_today=True, today="2026-07-27")
    assert daily_eval.should_evaluate(evaluated, "NVDA",
                                      "mean_reversion_reclaim",
                                      df_next, "2026-07-27") is True


def test_evaluation_is_per_ticker_and_strategy():
    evaluated = {}
    df = daily_df(5, include_today=True)
    daily_eval.mark_evaluated(evaluated, "NVDA", "mean_reversion_reclaim",
                              df, "2026-07-24")
    assert daily_eval.should_evaluate(evaluated, "AMD",
                                      "mean_reversion_reclaim",
                                      df, "2026-07-24") is True
    assert daily_eval.should_evaluate(evaluated, "NVDA",
                                      "momentum_continuation",
                                      df, "2026-07-24") is True


def test_gap_abort_auto_populates_from_signal_bar_mid():
    df = daily_df(5, include_today=True)
    # Signal bar (last completed): high 102, low 98 -> mid 100.
    # Today's open is 100.0 (not below mid) -> no abort.
    aborted, open_p, mid = daily_eval.gap_abort(df, current_price=100.5,
                                                today_str="2026-07-24")
    assert mid == 100.0 and open_p == 100.0 and aborted is False
    # Gap the session open below the mid -> abort.
    df.iloc[-1, df.columns.get_loc("open")] = 99.4
    aborted, open_p, mid = daily_eval.gap_abort(df, current_price=99.5,
                                                today_str="2026-07-24")
    assert aborted is True and open_p == 99.4 and mid == 100.0


def test_pre_open_uses_current_price():
    df = daily_df(5, include_today=False)      # no bar for today yet
    aborted, open_p, mid = daily_eval.gap_abort(df, current_price=97.0,
                                                today_str="2026-07-24")
    assert open_p == 97.0 and aborted is True  # current price below mid


def test_timeframes_config_and_trend_unchanged():
    config = {"strategy_timeframes": {"mean_reversion_reclaim": "daily",
                                      "momentum_continuation": "daily",
                                      "trend_continuation": "intraday"}}
    assert daily_eval.strategy_timeframe("mean_reversion_reclaim", config,
                                         "daily") == "daily"
    assert daily_eval.strategy_timeframe("trend_continuation", config,
                                         "intraday") == "intraday"
    # Old config without the key: class defaults hold, trend stays intraday.
    assert daily_eval.strategy_timeframe("trend_continuation", {},
                                         "intraday") == "intraday"
    assert daily_eval.strategy_timeframe("momentum_continuation", {},
                                         "daily") == "daily"
