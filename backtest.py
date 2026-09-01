"""
backtest.py — playbook expectancy over history (Goal 14).

    python backtest.py [--years 3] [--refresh] [--quick]

Replays the three strategy detectors + the deterministic filter stack over
2-3 years of DAILY bars for the full core_watchlist, using our EXACT trade
mechanics:

  - next-bar entry: signal evaluated on completed bars only; the fill is
    the NEXT bar's OPEN (never its close — no lookahead)
  - bracket exits: stop at thesis invalidation per strategy, 2R target;
    gap-throughs fill at the bar's open (worse than the level, like life)
  - whole shares, 25% notional cap, 1% risk on a $2,000 base
  - SPY-regime filter and Rule #3 gap-abort included; every deterministic
    rejection is tallied

Output: per (strategy x regime) trade count, win rate, avg R, expectancy,
max drawdown (R), profit factor — plus sensitivity sweeps (ADX threshold,
target R, stop placement). Human-readable verdicts in backtest_report.md.

Daily bars are cached in data/*.csv so reruns are free (--refresh refetches).
"""

import argparse
import json
import os
import sys
from datetime import datetime

import pandas as pd

import risk
from strategies import REGISTRY
from strategies.base import Signal

DATA_DIR = "data"
REPORT_FILE = "backtest_report.md"
BASE_EQUITY = 2000.0
RISK_PCT = 1.0
POSITION_CAP = 0.25
WARMUP_BARS = 40

BASE_RISK_PROFILE = {
    "fast_ema": 9, "slow_ema": 21, "adx_threshold": 28,
    "risk_per_trade_pct": RISK_PCT, "rr_ratio": 2.0, "atr_multiplier": 2.0,
    "trailing_stop_type": "ATR", "trailing_stop_value": 2.5,
    "use_volume_filter": True,
}
BASE_CONFIG = {"use_rsi_filter": True, "rsi_threshold": 55,
               "use_confirmation_candle": False}


# ============================================================ data layer

def load_daily(symbol: str, years: int = 3, broker=None,
               refresh: bool = False) -> pd.DataFrame:
    """Daily bars, cached as data/daily_<SYM>.csv. Reruns are free."""
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"daily_{symbol}.csv")
    if os.path.exists(path) and not refresh:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        if len(df) > 100:
            return df
    if broker is None:
        from broker import Broker
        broker = Broker()
    df = broker.get_daily_bars([symbol], lookback_days=int(years * 370)).get(symbol)
    if df is None or df.empty:
        return pd.DataFrame()
    df.to_csv(path)
    return df


def load_universe_bars(symbols, years=3, refresh=False) -> dict:
    from broker import Broker
    broker = Broker()
    out = {}
    missing = [s for s in symbols
               if refresh or not os.path.exists(
                   os.path.join(DATA_DIR, f"daily_{s}.csv"))]
    if missing:
        os.makedirs(DATA_DIR, exist_ok=True)
        fetched = broker.get_daily_bars(missing, lookback_days=int(years * 370))
        for s, df in fetched.items():
            if df is not None and not df.empty:
                df.to_csv(os.path.join(DATA_DIR, f"daily_{s}.csv"))
    for s in symbols:
        df = load_daily(s, years=years, broker=broker, refresh=False)
        if df is not None and len(df) > WARMUP_BARS + 10:
            out[s] = df
    return out


def spy_regime_series(spy_df: pd.DataFrame) -> pd.Series:
    """True = trending (SPY close above its 20d EMA), per completed bar."""
    ema20 = spy_df["close"].ewm(span=20, adjust=False).mean()
    return (spy_df["close"] > ema20)


# ============================================================ simulation

# =========================================================== short lane
# GOAL 20 — RESEARCH ONLY. These detectors exist in the backtest and NOWHERE
# else: orders.py still rejects SELL-to-open, and no live path imports them.
# A boardroom reviews these numbers before any live short exists.

def detect_breakdown_continuation(window: pd.DataFrame):
    """Mirror of momentum_continuation: close < prior 20-bar low on 1.5x
    volume and a daily change worse than -3%. Stop ABOVE the breakdown high."""
    if len(window) < 23:
        return None
    bar = window.iloc[-2]                       # last completed bar
    prior = window.iloc[-23:-2]
    prior_low = float(prior["low"].min())
    avg_vol = float(prior["volume"].mean())
    prev_close = float(window["close"].iloc[-3])
    change = ((float(bar["close"]) / prev_close) - 1) * 100 if prev_close else 0
    if float(bar["close"]) >= prior_low:
        return None
    if avg_vol <= 0 or float(bar["volume"]) <= avg_vol * 1.5:
        return "volume_low"
    if change >= -3.0:
        return "change_too_small"
    return {"stop_level": float(bar["high"]), "setup": "breakdown_continuation"}


def detect_failed_reclaim(window: pd.DataFrame):
    """Mirror of mean_reversion_reclaim: a name that rallied >= 10% off its
    20-bar low then closes back BELOW the prior day's low on 1.2x volume.
    Stop ABOVE the failure bar's high."""
    if len(window) < 23:
        return None
    bar = window.iloc[-2]
    prior_bar = window.iloc[-3]
    w = window.iloc[-23:-2]
    low_20 = float(w["low"].min())
    high_since = float(w["high"].max())
    if low_20 <= 0:
        return None
    runup = (high_since - low_20) / low_20 * 100
    if runup < 10.0 or float(bar["close"]) >= float(prior_bar["low"]):
        return None
    avg_vol = float(w["volume"].mean())
    if avg_vol <= 0 or float(bar["volume"]) <= avg_vol * 1.2:
        return "volume_low"
    return {"stop_level": float(bar["high"]), "setup": "failed_reclaim"}


SHORT_DETECTORS = {"breakdown_continuation": detect_breakdown_continuation,
                   "failed_reclaim": detect_failed_reclaim}


def simulate_bracket_short(df: pd.DataFrame, entry_i: int, entry: float,
                           stop: float, target: float):
    """Short mechanics mirrored: stop ABOVE entry, target BELOW."""
    for i in range(entry_i, len(df)):
        bar = df.iloc[i]
        o, h, l = float(bar["open"]), float(bar["high"]), float(bar["low"])
        if i > entry_i and o >= stop:
            return o, i, "gap_stop"
        if i > entry_i and o <= target:
            return o, i, "gap_target"
        if h >= stop:
            return stop, i, "stop"
        if l <= target:
            return target, i, "target"
    return None, None, "open"


def replay_short(symbol: str, df: pd.DataFrame, name: str, regime: pd.Series,
                 target_r: float = 2.0) -> list:
    detector = SHORT_DETECTORS[name]
    trades, busy_until = [], -1
    for i in range(WARMUP_BARS, len(df)):
        if i <= busy_until:
            continue
        window = df.iloc[max(0, i - DETECT_WINDOW + 1):i + 1]
        result = detector(window)
        if not isinstance(result, dict):
            continue
        entry = float(df["open"].iloc[i])
        stop = result["stop_level"]
        if stop <= entry:
            continue
        target = entry - (stop - entry) * target_r
        qty = risk.position_size(BASE_EQUITY, RISK_PCT, stop, entry,
                                 position_cap_pct=POSITION_CAP)  # risk/share = stop-entry
        if qty < 1:
            continue
        exit_price, exit_i, reason = simulate_bracket_short(df, i, entry, stop,
                                                            target)
        if exit_price is None:
            break
        busy_until = exit_i
        r_mult = (entry - exit_price) / (stop - entry)     # short P/L
        sig_day = df.index[i - 1]
        in_regime = bool(regime.reindex([sig_day]).fillna(False).iloc[0]) \
            if regime is not None else True
        trades.append({
            "symbol": symbol, "strategy": name,
            "signal_date": str(sig_day)[:10],
            "entry_date": str(df.index[i])[:10],
            "exit_date": str(df.index[exit_i])[:10],
            "entry": round(entry, 2), "stop": round(stop, 2),
            "target": round(target, 2), "exit": round(float(exit_price), 2),
            "qty": qty, "r": round(r_mult, 3),
            "pnl_usd": round((entry - exit_price) * qty, 2),
            "exit_reason": reason,
            "regime": "trending" if in_regime else "chop",
            "bars_held": exit_i - i,
        })
    return trades


def simulate_bracket(df: pd.DataFrame, entry_i: int, entry: float,
                     stop: float, target: float):
    """Walk forward from the entry bar. Returns (exit_price, exit_i, reason)
    or (None, None, 'open') if the position never exits in-sample.

    Fill rules (conservative, gap-aware):
      - entry bar itself can stop out or hit target after the open
      - a bar that OPENS through a level fills at that open, not the level
      - if a single bar spans both levels, the STOP is assumed to fill first
    """
    for i in range(entry_i, len(df)):
        bar = df.iloc[i]
        o, h, l = float(bar["open"]), float(bar["high"]), float(bar["low"])
        if i > entry_i and o <= stop:
            return o, i, "gap_stop"
        if i > entry_i and o >= target:
            return o, i, "gap_target"
        if l <= stop:
            return stop, i, "stop"
        if h >= target:
            return target, i, "target"
    return None, None, "open"


DETECT_WINDOW = 60   # bars of context per detection (indicators need ~40)


def collect_signals(symbol: str, df: pd.DataFrame, strat_name: str,
                    regime: pd.Series, risk_profile: dict, config: dict):
    """Scan one symbol once. No lookahead: the detector sees a sliding
    window ending at iloc[i], where iloc[i] is the ENTRY bar (only its open
    is ever used for the fill); the signal bar iloc[i-1] is fully completed.
    Returns (signals, rejections) — exits are simulated separately so
    target-R sweeps don't re-run detection."""
    strat = REGISTRY[strat_name]()
    signals, rejections = [], {}

    def rej(name):
        rejections[name] = rejections.get(name, 0) + 1

    context = {"ticker": symbol, "risk_profile": dict(risk_profile),
               "config": dict(config)}
    for i in range(WARMUP_BARS, len(df)):
        window = df.iloc[max(0, i - DETECT_WINDOW + 1):i + 1]
        try:
            result = strat.detect(window, context)
        except Exception:
            continue
        if result is None:
            continue
        if not isinstance(result, Signal):
            rej(result.filter_name)
            continue

        # SPY regime is a LABEL here, not a gate: the live loop rejects
        # chop-regime signals, but the backtest must measure both sides so
        # the chop column shows what the SPY filter is suppressing —
        # that comparison is the evidence for (or against) the filter.
        sig_day = df.index[i - 1]
        in_regime = bool(regime.reindex([sig_day]).fillna(False).iloc[0]) \
            if regime is not None else True

        # ---- next-bar entry mechanics: fill at the ENTRY bar's OPEN ----
        entry = float(df["open"].iloc[i])
        stop_dist = result.entry - result.stop      # signal-bar-anchored
        if strat_name == "trend_continuation":
            stop = entry - stop_dist                # ATR distance from fill
        else:
            stop = result.stop                      # absolute bar-low level
        if stop >= entry:
            rej("stop_above_entry_at_open")
            continue
        qty = risk.position_size(BASE_EQUITY, RISK_PCT, entry, stop,
                                 position_cap_pct=POSITION_CAP)
        if qty < 1:
            rej(risk.zero_size_reason(entry, BASE_EQUITY,
                                      position_cap_pct=POSITION_CAP))
            continue
        signals.append({"entry_i": i, "entry": entry, "stop": stop,
                        "qty": qty, "sig_day": sig_day,
                        "regime": "trending" if in_regime else "chop"})
    return signals, rejections


def trades_from_signals(symbol: str, df: pd.DataFrame, strat_name: str,
                        signals: list, target_r: float) -> list:
    """Simulate bracket exits for collected signals at a given target R.
    One position per symbol: signals during an open trade are skipped."""
    trades, busy_until = [], -1
    for s in signals:
        i = s["entry_i"]
        if i <= busy_until:
            continue
        entry, stop = s["entry"], s["stop"]
        target = entry + (entry - stop) * target_r
        exit_price, exit_i, reason = simulate_bracket(df, i, entry, stop, target)
        if exit_price is None:
            break                                   # still open at data end
        busy_until = exit_i
        r_mult = (exit_price - entry) / (entry - stop)
        trades.append({
            "symbol": symbol, "strategy": strat_name,
            "signal_date": str(s["sig_day"])[:10],
            "entry_date": str(df.index[i])[:10],
            "exit_date": str(df.index[exit_i])[:10],
            "entry": round(entry, 2), "stop": round(stop, 2),
            "target": round(target, 2), "exit": round(float(exit_price), 2),
            "qty": s["qty"], "r": round(r_mult, 3),
            "pnl_usd": round((exit_price - entry) * s["qty"], 2),
            "exit_reason": reason, "regime": s["regime"],
            "bars_held": exit_i - i,
        })
    return trades


def replay_symbol(symbol: str, df: pd.DataFrame, strat_name: str,
                  regime: pd.Series, risk_profile: dict, config: dict,
                  target_r: float = 2.0):
    """Convenience: collect + simulate in one call (used by tests)."""
    signals, rejections = collect_signals(symbol, df, strat_name, regime,
                                          risk_profile, config)
    return trades_from_signals(symbol, df, strat_name, signals,
                               target_r), rejections


# ======================================================= research lane
# WORK ORDER 2026-09-01 item 8 — RESEARCH ONLY. Like the short lane, these
# detectors live in backtest.py and NOWHERE else: they are not in
# strategies/REGISTRY, no live path imports them, and nothing can trade them.
# A boardroom reviews these numbers before any live wiring exists.

PULLBACK_LOOKBACK = 12      # bars of pullback inspected before the trigger
PEC_GAP_PCT = 5.0           # minimum gap-up
PEC_GAP_VOL_MULT = 2.0      # gap must come on volume
PEC_WINDOW = 5              # trigger must arrive within 5 sessions of the gap
PEC_MAX_HOLD = 55           # < one quarter of sessions: never held into a print


def detect_pullback_in_uptrend(window: pd.DataFrame):
    """Price above RISING 20/50-day EMAs, a pullback to the 20-day EMA on
    DECLINING volume, entry on the first close back above the prior day's
    high. Stop under the pullback low.

    window[-1] is the entry bar (only its open is ever used); window[-2] is
    the completed trigger bar. No lookahead."""
    if len(window) < 55:
        return None
    hist = window.iloc[:-1]                       # completed bars only
    close = hist["close"]
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    bar, prior = hist.iloc[-1], hist.iloc[-2]
    e20, e50 = float(ema20.iloc[-1]), float(ema50.iloc[-1])

    # 1. uptrend: stacked AND rising (a flat 20>50 is not an uptrend)
    if e20 <= e50:
        return None
    if not (e20 > float(ema20.iloc[-6]) and e50 > float(ema50.iloc[-6])):
        return "emas_not_rising"
    if float(bar["close"]) <= e20:
        return "close_not_back_above_ema20"

    # 2. a real pullback: price actually traded down to the 20-day EMA
    look = hist.iloc[-(PULLBACK_LOOKBACK + 1):-1]
    e20_look = ema20.iloc[-(PULLBACK_LOOKBACK + 1):-1]
    if len(look) < 4:
        return None
    if not bool((look["low"].values <= e20_look.values * 1.01).any()):
        return "no_pullback_to_ema20"

    # 3. the pullback must be ORDERLY — supply drying up, not distribution
    pull_vol = float(look["volume"].mean())
    base = hist["volume"].iloc[-(PULLBACK_LOOKBACK + 21):-(PULLBACK_LOOKBACK + 1)]
    base_vol = float(base.mean()) if len(base) else 0.0
    if base_vol <= 0 or pull_vol >= base_vol:
        return "pullback_volume_not_declining"

    # 4. trigger: the FIRST close back above the prior day's high SINCE the
    #    pullback low. "First" has to be anchored to the pullback, not to
    #    yesterday — in a smooth grind-up every bar closes above the prior
    #    high, and anchoring on yesterday alone rejects the real trigger.
    if float(bar["close"]) <= float(prior["high"]):
        return "no_reclaim_of_prior_high"
    lows = look["low"].values
    low_pos = int(lows.argmin())
    since = hist.iloc[-(PULLBACK_LOOKBACK + 1) + low_pos + 1:-1]
    if len(since) >= 2:
        prior_highs = since["high"].shift(1)
        if bool((since["close"].iloc[1:] > prior_highs.iloc[1:]).any()):
            return "not_first_trigger"

    stop = float(look["low"].min())
    return {"stop_level": stop, "setup": "pullback_in_uptrend"}


def detect_post_earnings_continuation(window: pd.DataFrame):
    """Gap-up >= 5% on >= 2x volume, entry on the first close above the
    gap day's high within 5 sessions, stop under the gap day's low.

    HONEST LIMITATION: the work order specifies a VERIFIED EARNINGS BEAT.
    We have no earnings calendar and no estimates feed — Alpaca gives us
    bars, not fundamentals — so "verified beat" is NOT tested here. What is
    tested is its observable shadow: a >=5% gap on >=2x volume. That set
    contains earnings beats and also contains M&A pops, guidance raises and
    sector news. Read every number below as the gap-up class, not the
    earnings class, until a fundamentals feed exists."""
    if len(window) < 30:
        return None
    hist = window.iloc[:-1]
    bar = hist.iloc[-1]
    for back in range(1, PEC_WINDOW + 1):
        if len(hist) < 27 + back:
            break
        gap_bar, prev = hist.iloc[-1 - back], hist.iloc[-2 - back]
        prev_close = float(prev["close"])
        if prev_close <= 0:
            continue
        if (float(gap_bar["open"]) / prev_close - 1) * 100 < PEC_GAP_PCT:
            continue
        base = hist["volume"].iloc[-21 - back:-1 - back]
        base_vol = float(base.mean()) if len(base) else 0.0
        if base_vol <= 0 or float(gap_bar["volume"]) < base_vol * PEC_GAP_VOL_MULT:
            continue

        gap_high, gap_low = float(gap_bar["high"]), float(gap_bar["low"])
        if float(bar["close"]) <= gap_high:
            return "no_close_above_gap_high"
        between = hist.iloc[-back:]
        if len(between) > 1 and bool((between["close"].iloc[:-1] > gap_high).any()):
            return "not_first_close_above"
        return {"stop_level": gap_low, "setup": "post_earnings_continuation",
                "max_hold": PEC_MAX_HOLD}
    return None


RESEARCH_DETECTORS = {
    "pullback_in_uptrend": detect_pullback_in_uptrend,
    "post_earnings_continuation": detect_post_earnings_continuation,
}
RESEARCH_TARGET_R = 3.0


def replay_research(symbol: str, df: pd.DataFrame, name: str,
                    regime: pd.Series, target_r: float = RESEARCH_TARGET_R,
                    detectors: dict = None) -> list:
    """Long-side replay for the research detectors, using the SAME mechanics
    as the live playbook: next-bar-open entry, bracket exit, whole shares,
    1% risk on $2,000, 25% cap, one position per symbol at a time."""
    detector = (detectors or RESEARCH_DETECTORS)[name]
    trades, busy_until = [], -1
    for i in range(WARMUP_BARS, len(df)):
        if i <= busy_until:
            continue
        window = df.iloc[max(0, i - DETECT_WINDOW + 1):i + 1]
        result = detector(window)
        if not isinstance(result, dict):
            continue
        entry = float(df["open"].iloc[i])
        stop = float(result["stop_level"])
        if stop >= entry:
            continue
        target = entry + (entry - stop) * target_r
        qty = risk.position_size(BASE_EQUITY, RISK_PCT, entry, stop,
                                 position_cap_pct=POSITION_CAP)
        if qty < 1:
            continue
        exit_price, exit_i, reason = simulate_bracket(df, i, entry, stop, target)
        max_hold = result.get("max_hold")
        held_too_long = (max_hold and exit_i is not None
                         and exit_i - i > max_hold)
        if exit_price is None or held_too_long:
            # Time stop: the setup never holds through the next print.
            cap_i = min(i + max_hold, len(df) - 1) if max_hold else None
            if cap_i is None or cap_i <= i:
                break
            exit_price = float(df["close"].iloc[cap_i])
            exit_i, reason = cap_i, "time_stop"
        busy_until = exit_i
        r_mult = (exit_price - entry) / (entry - stop)
        sig_day = df.index[i - 1]
        in_regime = bool(regime.reindex([sig_day]).fillna(False).iloc[0]) \
            if regime is not None else True
        trades.append({
            "symbol": symbol, "strategy": name,
            "signal_date": str(sig_day)[:10],
            "entry_date": str(df.index[i])[:10],
            "exit_date": str(df.index[exit_i])[:10],
            "entry": round(entry, 2), "stop": round(stop, 2),
            "target": round(target, 2), "exit": round(float(exit_price), 2),
            "qty": qty, "r": round(r_mult, 3),
            "pnl_usd": round((exit_price - entry) * qty, 2),
            "exit_reason": reason,
            "regime": "trending" if in_regime else "chop",
            "bars_held": exit_i - i,
        })
    return trades



# ============================================================ metrics

def aggregate(trades: list) -> dict:
    if not trades:
        return {"trades": 0, "win_rate": None, "avg_r": None,
                "expectancy_r": None, "profit_factor": None,
                "max_drawdown_r": None, "avg_pnl_usd": None}
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [-r for r in rs if r <= 0]
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "trades": len(rs),
        "win_rate": round(100 * len(wins) / len(rs), 1),
        "avg_r": round(sum(rs) / len(rs), 3),
        "expectancy_r": round(sum(rs) / len(rs), 3),
        "profit_factor": round(sum(wins) / sum(losses), 2) if losses and sum(losses) > 0
        else (float("inf") if wins else 0.0),
        "max_drawdown_r": round(max_dd, 2),
        "avg_pnl_usd": round(sum(t["pnl_usd"] for t in trades) / len(rs), 2),
    }


def verdict_line(name: str, by_regime: dict) -> str:
    t = by_regime.get("trending", {})
    c = by_regime.get("chop", {})

    def word(stats):
        e = stats.get("expectancy_r")
        if e is None:
            return "no trades"
        if e > 0.15:
            return f"POSITIVE expectancy ({e:+.2f}R)"
        if e > 0:
            return f"marginal ({e:+.2f}R)"
        return f"NEGATIVE ({e:+.2f}R)"
    return (f"**{name}**: {word(t)} in trending regime "
            f"({t.get('trades', 0)} trades), {word(c)} in chop "
            f"({c.get('trades', 0)} trades).")


# ============================================================ runner

def run_backtest(symbols, years=3, refresh=False, sweeps=True):
    import clockline
    bars = load_universe_bars(list(symbols) + ["SPY"], years=years,
                              refresh=refresh)
    spy = bars.pop("SPY", None)
    regime = spy_regime_series(spy) if spy is not None else None
    strategies = list(REGISTRY.keys())

    def collect_pass(risk_profile, config, only_strategy=None):
        sigs, all_rej = {}, {}
        for strat_name in ([only_strategy] if only_strategy else strategies):
            for sym, df in bars.items():
                s, rej = collect_signals(sym, df, strat_name, regime,
                                         risk_profile, config)
                sigs[(strat_name, sym)] = s
                for k, v in rej.items():
                    all_rej[k] = all_rej.get(k, 0) + v
        return sigs, all_rej

    def sim_pass(sigs, target_r):
        out = []
        for (strat_name, sym), s in sigs.items():
            out.extend(trades_from_signals(sym, bars[sym], strat_name, s,
                                           target_r))
        return out

    base_sigs, base_rej = collect_pass(BASE_RISK_PROFILE, BASE_CONFIG)
    base_trades = sim_pass(base_sigs, 2.0)

    lines = ["# Backtest report", clockline.two_zone_line(), "",
             f"Universe: {len(bars)} tickers | {years}y daily bars | "
             f"base: 1% risk on $2,000, 25% cap, whole shares, "
             f"next-bar-open entries, bracket exits", ""]

    lines.append("## Headline verdicts")
    per_strat_regime = {}
    for s in strategies:
        by_regime = {}
        for reg in ("trending", "chop"):
            by_regime[reg] = aggregate(
                [t for t in base_trades
                 if t["strategy"] == s and t["regime"] == reg])
        per_strat_regime[s] = by_regime
        lines.append("- " + verdict_line(s, by_regime))

    lines.append("\n## Per strategy x regime (base config, 2R target)")
    lines.append("| Strategy | Regime | Trades | Win% | Avg R | Expectancy | PF | MaxDD (R) | Avg $ |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for s in strategies:
        for reg in ("trending", "chop"):
            st = per_strat_regime[s][reg]
            lines.append(
                f"| {s} | {reg} | {st['trades']} | {st['win_rate']} "
                f"| {st['avg_r']} | {st['expectancy_r']} | {st['profit_factor']} "
                f"| {st['max_drawdown_r']} | {st['avg_pnl_usd']} |")

    lines.append("\n## Deterministic filter rejections (base pass)")
    for k, v in sorted(base_rej.items(), key=lambda kv: -kv[1]):
        lines.append(f"- {k}: {v}")

    if sweeps:
        lines.append("\n## Sensitivity: ADX threshold (trend_continuation)")
        lines.append("| ADX >= | Trades | Win% | Expectancy (R) | PF |")
        lines.append("|---|---|---|---|---|")
        for adx in (20, 25, 28, 30):
            rp = dict(BASE_RISK_PROFILE, adx_threshold=adx)
            sigs, _ = collect_pass(rp, BASE_CONFIG,
                                   only_strategy="trend_continuation")
            st = aggregate(sim_pass(sigs, 2.0))
            lines.append(f"| {adx} | {st['trades']} | {st['win_rate']} "
                         f"| {st['expectancy_r']} | {st['profit_factor']} |")

        lines.append("\n## Sensitivity: target R (all strategies, base signals)")
        lines.append("| Target | Strategy | Trades | Win% | Expectancy (R) | PF |")
        lines.append("|---|---|---|---|---|---|")
        for tr in (1.5, 2.0, 3.0):
            trades = sim_pass(base_sigs, tr)     # detection reused — free
            for s in strategies:
                st = aggregate([t for t in trades if t["strategy"] == s])
                lines.append(f"| {tr}R | {s} | {st['trades']} | {st['win_rate']} "
                             f"| {st['expectancy_r']} | {st['profit_factor']} |")

        lines.append("\n## Sensitivity: stop placement (ATR mult, "
                     "trend_continuation; momentum + reclaim are bar-low natively)")
        lines.append("| Stop mode | Trades | Win% | Expectancy (R) |")
        lines.append("|---|---|---|---|")
        for mult in (1.5, 2.0):
            rp = dict(BASE_RISK_PROFILE, atr_multiplier=mult)
            sigs, _ = collect_pass(rp, BASE_CONFIG,
                                   only_strategy="trend_continuation")
            st = aggregate(sim_pass(sigs, 2.0))
            lines.append(f"| ATR x{mult} | {st['trades']} "
                         f"| {st['win_rate']} | {st['expectancy_r']} |")

    # ---- Work order 2026-09-01 item 8: NEW SETUP RESEARCH (never live) ----
    lines.append("\n## New setup research — BACKTEST ONLY (not wired live)")
    lines.append("_Neither setup is in strategies/REGISTRY; no live path can "
                 "reach them. 3R target, next-bar-open entry, same sizing and "
                 "bracket mechanics as the live playbook._")
    lines.append("")
    lines.append("**post_earnings_continuation caveat:** the specification "
                 "says *verified beat*. We have no earnings calendar and no "
                 "estimates feed, so the beat is NOT tested — only its "
                 "observable shadow (a >=5% gap on >=2x volume), which also "
                 "contains M&A pops, guidance raises and sector news. Read "
                 "the rows below as the gap-up class, not the earnings class. "
                 f"Holds are capped at {PEC_MAX_HOLD} sessions so no trade "
                 "spans the next print.")
    research_all = []
    for name in RESEARCH_DETECTORS:
        for sym, df in bars.items():
            research_all.extend(replay_research(sym, df, name, regime,
                                                RESEARCH_TARGET_R))
    lines.append("\n### Per setup x regime (3R target)")
    lines.append("| Setup | Regime | Trades | Win% | Avg R | Expectancy | PF "
                 "| MaxDD (R) | Avg $ |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for name in RESEARCH_DETECTORS:
        for reg in ("trending", "chop"):
            st = aggregate([t for t in research_all
                            if t["strategy"] == name and t["regime"] == reg])
            lines.append(f"| {name} | {reg} | {st['trades']} | {st['win_rate']} "
                         f"| {st['avg_r']} | {st['expectancy_r']} "
                         f"| {st['profit_factor']} | {st['max_drawdown_r']} "
                         f"| {st['avg_pnl_usd']} |")

    lines.append("\n### Target-R sensitivity (research setups)")
    lines.append("| Target | Setup | Trades | Win% | Expectancy (R) | PF |")
    lines.append("|---|---|---|---|---|---|")
    for tr in (2.0, 3.0, 4.0):
        for name in RESEARCH_DETECTORS:
            rows = []
            for sym, df in bars.items():
                rows.extend(replay_research(sym, df, name, regime, tr))
            st = aggregate(rows)
            lines.append(f"| {tr}R | {name} | {st['trades']} | {st['win_rate']} "
                         f"| {st['expectancy_r']} | {st['profit_factor']} |")

    lines.append("\n### Exit-reason mix (3R target)")
    lines.append("| Setup | target | stop | gap_target | gap_stop | time_stop |")
    lines.append("|---|---|---|---|---|---|")
    for name in RESEARCH_DETECTORS:
        mine = [t for t in research_all if t["strategy"] == name]
        counts = {k: sum(1 for t in mine if t["exit_reason"] == k)
                  for k in ("target", "stop", "gap_target", "gap_stop",
                            "time_stop")}
        lines.append(f"| {name} | " + " | ".join(
            str(counts[k]) for k in ("target", "stop", "gap_target",
                                     "gap_stop", "time_stop")) + " |")

    lines.append("")
    for name in RESEARCH_DETECTORS:
        by_regime = {reg: aggregate([t for t in research_all
                                     if t["strategy"] == name
                                     and t["regime"] == reg])
                     for reg in ("trending", "chop")}
        lines.append("- " + verdict_line(name, by_regime))

    # ---- Goal 20: short-lane RESEARCH (backtest only, never live) ----
    lines.append("\n## Short lane — RESEARCH ONLY (not wired live)")
    lines.append("_orders.py still rejects SELL-to-open. A boardroom reviews "
                 "these numbers before any live short exists._")
    lines.append("| Strategy | Regime | Trades | Win% | Avg R | Expectancy | PF | MaxDD (R) |")
    lines.append("|---|---|---|---|---|---|---|---|")
    short_all = []
    for name in SHORT_DETECTORS:
        for sym, df in bars.items():
            short_all.extend(replay_short(sym, df, name, regime, 2.0))
    for name in SHORT_DETECTORS:
        for reg in ("trending", "chop"):
            st = aggregate([t for t in short_all
                            if t["strategy"] == name and t["regime"] == reg])
            lines.append(f"| {name} | {reg} | {st['trades']} | {st['win_rate']} "
                         f"| {st['avg_r']} | {st['expectancy_r']} "
                         f"| {st['profit_factor']} | {st['max_drawdown_r']} |")
    for name in SHORT_DETECTORS:
        by_regime = {reg: aggregate([t for t in short_all
                                     if t["strategy"] == name and t["regime"] == reg])
                     for reg in ("trending", "chop")}
        lines.append("- " + verdict_line(name, by_regime))

    report = "\n".join(lines) + "\n"
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(report)
    return base_trades, report


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--years", type=int, default=3)
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--quick", action="store_true",
                   help="skip sensitivity sweeps")
    args = p.parse_args()

    try:
        with open("bot_config.json") as f:
            watchlist = json.load(f).get("universe", {}).get("core_watchlist", [])
    except (FileNotFoundError, json.JSONDecodeError):
        watchlist = []
    if not watchlist:
        print("No core_watchlist in bot_config.json — nothing to backtest.")
        return 1

    trades, report = run_backtest(watchlist, years=args.years,
                                  refresh=args.refresh, sweeps=not args.quick)
    print(report)
    print(f"\n{len(trades)} closed trades (base pass). Written: {REPORT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
