"""
daily_eval.py — once-per-completed-session evaluation for daily strategies.

The playbook is written in daily bars (Goal 15): mean_reversion_reclaim and
momentum_continuation evaluate ONCE per completed session — when a new daily
bar exists — not on every 30s cycle. trend_continuation stays on 5-min.

Rule #3 is default for daily entries: the entry is aborted if the session
OPEN is below the signal bar's midpoint (auto-set, no sheet field needed).
"""

from datetime import datetime

import pytz

EASTERN_TZ = pytz.timezone("US/Eastern")

# Config override (bot_config.json "strategy_timeframes"); class attrs are
# the fallback so old configs keep working.
DEFAULT_TIMEFRAMES = {
    "mean_reversion_reclaim": "daily",
    "momentum_continuation": "daily",
    "trend_continuation": "intraday",
}


def strategy_timeframe(strat_name: str, config: dict, class_default: str) -> str:
    return (config or {}).get("strategy_timeframes", {}).get(
        strat_name, DEFAULT_TIMEFRAMES.get(strat_name, class_default))


def completed_bar_date(daily_df, today_str: str = None):
    """Date string of the last COMPLETED daily bar (today's partial bar,
    when present, is not it). None if nothing usable."""
    if daily_df is None or len(daily_df) == 0:
        return None
    today_str = today_str or datetime.now(EASTERN_TZ).strftime("%Y-%m-%d")
    last_day = str(daily_df.index[-1])[:10]
    if last_day == today_str:
        return str(daily_df.index[-2])[:10] if len(daily_df) >= 2 else None
    return last_day


def should_evaluate(evaluated: dict, ticker: str, strat_name: str,
                    daily_df, today_str: str = None) -> bool:
    """True exactly once per (ticker, strategy, completed bar). The caller
    must call mark_evaluated() afterwards regardless of the outcome."""
    bar_date = completed_bar_date(daily_df, today_str)
    if bar_date is None:
        return False
    return evaluated.get((ticker, strat_name)) != bar_date


def mark_evaluated(evaluated: dict, ticker: str, strat_name: str,
                   daily_df, today_str: str = None):
    bar_date = completed_bar_date(daily_df, today_str)
    if bar_date is not None:
        evaluated[(ticker, strat_name)] = bar_date


def signal_bar_midpoint(daily_df, today_str: str = None):
    """Midpoint of the SIGNAL bar (the last completed bar): Rule #3's
    auto-set abort level for queued daily entries."""
    if daily_df is None or len(daily_df) < 1:
        return None
    today_str = today_str or datetime.now(EASTERN_TZ).strftime("%Y-%m-%d")
    last_day = str(daily_df.index[-1])[:10]
    bar = daily_df.iloc[-2] if (last_day == today_str and len(daily_df) >= 2) \
        else daily_df.iloc[-1]
    return (float(bar["high"]) + float(bar["low"])) / 2.0


def session_open_price(daily_df, current_price: float,
                       today_str: str = None) -> float:
    """Today's session open when today's bar exists; else the freshest price
    we have (pre-open evaluation)."""
    today_str = today_str or datetime.now(EASTERN_TZ).strftime("%Y-%m-%d")
    if daily_df is not None and len(daily_df) and \
            str(daily_df.index[-1])[:10] == today_str:
        return float(daily_df["open"].iloc[-1])
    return current_price


def gap_abort(daily_df, current_price: float, today_str: str = None):
    """Returns (aborted: bool, open_price, midpoint). Rule #3 default for
    daily entries: abort when the session open is below the signal bar mid."""
    mid = signal_bar_midpoint(daily_df, today_str)
    if mid is None:
        return False, None, None
    open_price = session_open_price(daily_df, current_price, today_str)
    return open_price < mid, open_price, mid


# --- SPY regime on COMPLETED daily bars (2026-09-02) -----------------------
# The live filter used to read a 20-period EMA of FIVE-MINUTE SPY bars, i.e.
# a ~100-minute average, while the backtest that justified the filter used a
# 20-DAY EMA of daily closes (backtest.spy_regime_series). They are different
# indicators, so the regime the desk enforced was never the regime the
# evidence was measured on. This computes the backtest's definition, on the
# last COMPLETED session only — today's partial daily bar is never used,
# matching the completed-bar semantics every daily signal already follows.
SPY_EMA_SPAN = 20


def spy_regime(spy_daily_df, today_str: str = None):
    """Regime from the last COMPLETED SPY session.

    Returns {'trending': bool, 'spy_close': float, 'ema20d': float,
    'as_of': 'YYYY-MM-DD'}, or None when the data cannot support a verdict.
    None means UNKNOWN — the caller decides policy; this never guesses."""
    if spy_daily_df is None or len(spy_daily_df) < 2:
        return None
    as_of = completed_bar_date(spy_daily_df, today_str)
    if as_of is None:
        return None
    closes = spy_daily_df["close"]
    # Drop today's partial bar before averaging: including it would let an
    # intraday wobble move the 20-day EMA that gates every entry.
    last_day = str(spy_daily_df.index[-1])[:10]
    today_str = today_str or datetime.now(EASTERN_TZ).strftime("%Y-%m-%d")
    if last_day == today_str:
        closes = closes.iloc[:-1]
    if len(closes) < 2:
        return None
    ema = closes.ewm(span=SPY_EMA_SPAN, adjust=False).mean()
    spy_close = float(closes.iloc[-1])
    ema20d = float(ema.iloc[-1])
    return {"trending": bool(spy_close > ema20d), "spy_close": spy_close,
            "ema20d": ema20d, "as_of": as_of}


def regime_details(regime: dict) -> str:
    """The evidence string appended to every regime-gated journal row, so a
    later audit can recompute the verdict instead of trusting it."""
    if not regime:
        return "spy_close=? ema20d=? regime=unknown as_of=?"
    return (f"spy_close={regime['spy_close']:.2f} "
            f"ema20d={regime['ema20d']:.2f} "
            f"regime={'trending' if regime['trending'] else 'chop'} "
            f"as_of={regime['as_of']}")
