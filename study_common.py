"""
study_common.py — shared harness for the W3 boardroom studies.

Read-only. Loads cached daily bars, replays the live detectors, attaches the
features each study buckets on (ADX, volume ratio), and simulates exits.
Nothing here writes to bot_config.json, the journal, or any strategy file.

TWO THINGS EVERY STUDY HERE HAS TO WORK AROUND
----------------------------------------------
1. Bands below the live threshold do not exist in the live population. The
   backtest runs adx_threshold=28 and the live config runs a 1.3x volume
   multiplier, so a signal at ADX 26 or 1.1x volume was never generated and
   cannot be counted. To measure those bands the study LOWERS its own
   threshold (study-local risk profile only) to admit the wider population,
   then buckets by the value actually observed on the signal bar.

2. Everything below is measured on the SIGNAL, not on a trade the desk took.
   A signal that would have been admitted still had to clear the AI
   gatekeeper, whole-share sizing on a $2,000 cap, max-positions and the
   daily-loss breaker. Expectancies here are therefore an upper bound on
   what the desk would have realised.
"""

import json
import math

import pandas as pd
import pandas_ta as ta

import backtest
import risk

YEARS = 3


# ============================================================ data

def load_config():
    with open("bot_config.json", encoding="utf-8") as f:
        return json.load(f)


def watchlist():
    return load_config().get("universe", {}).get("core_watchlist", [])


def load_bars(symbols=None, years: int = YEARS, refresh: bool = False):
    """{symbol: daily df} plus the SPY regime series, from the CSV cache."""
    symbols = list(symbols or watchlist())
    bars = backtest.load_universe_bars(symbols + ["SPY"], years=years,
                                       refresh=refresh)
    spy = bars.pop("SPY", None)
    regime = backtest.spy_regime_series(spy) if spy is not None else None
    return bars, regime


def study_profile(**over):
    """A COPY of the backtest's base profile. Never mutate the original."""
    profile = dict(backtest.BASE_RISK_PROFILE)
    profile.update(over)
    return profile


def study_config(**over):
    config = json.loads(json.dumps(backtest.BASE_CONFIG))
    config.update(over)
    return config


# ============================================================ features

def adx_at(df, pos: int):
    """ADX(14) on the signal bar at integer position `pos`, computed the way
    the DETECTOR computes it.

    ADX is Wilder-smoothed and therefore window-dependent: the same bar
    scores differently over a 60-bar window than over 750 bars of history.
    The live detector only ever sees backtest.DETECT_WINDOW bars ending at
    the ENTRY bar, so the study must reproduce exactly that slice, or it
    buckets signals by a number the desk never saw. (Measured: doing this on
    the full series left 35 of 62 trades outside both bands.)"""
    try:
        end = pos + 1                       # entry bar, as the detector sees it
        start = max(0, end - backtest.DETECT_WINDOW + 1)
        window = df.iloc[start:end + 1]
        if len(window) < 20:
            return None
        series = ta.adx(window["high"], window["low"], window["close"],
                        length=14)
        if series is None or series.empty:
            return None
        col = [c for c in series.columns if c.startswith("ADX")][0]
        value = float(series[col].iloc[-2])   # the signal bar, not the entry
        return None if math.isnan(value) else value
    except Exception:
        return None


def volume_ratio_at(df, pos: int, lookback: int = 20):
    """Signal-bar volume over its trailing average, or None."""
    if pos < lookback:          # need `lookback` bars BEFORE pos, i.e. 0..pos-1
        return None
    window = df["volume"].iloc[pos - lookback:pos]
    avg = float(window.mean())
    if avg <= 0:
        return None
    return float(df["volume"].iloc[pos]) / avg


def collect_with_features(bars, regime, strat_name, profile, config,
                          feature_fns=None):
    """Every signal the detector produces, with per-signal features attached.

    The signal bar is entry_i - 1: collect_signals fills at the ENTRY bar's
    open, so the bar the detector actually judged is the one before it.
    Features are read there — never on the entry bar, which would be
    lookahead."""
    feature_fns = feature_fns or {}
    out = []
    for symbol, df in bars.items():
        signals, _ = backtest.collect_signals(symbol, df, strat_name, regime,
                                              profile, config)
        for sig in signals:
            pos = sig["entry_i"] - 1
            row = dict(sig, symbol=symbol)
            for name, fn in feature_fns.items():
                row[name] = fn(df, pos)
            out.append(row)
    return out


# ============================================================ exits

def atr_series(df, length: int = 14):
    try:
        series = ta.atr(df["high"], df["low"], df["close"], length=length)
        return None if series is None or series.dropna().empty else series
    except Exception:
        return None


def simulate_exit(df, entry_i, entry, stop, target, mode="static",
                  atr=None, atr_mult=2.5):
    """Walk forward from the entry bar. Returns (exit_price, exit_i, reason).

    Fill conventions match backtest.simulate_bracket exactly, so the three
    modes differ ONLY in where the stop sits:
      - a bar opening through a level fills at that open, not the level
      - a bar spanning both levels is assumed to hit the STOP first

    modes:
      static  — the structural stop never moves (the pre-2026-09-03 backtest)
      atr     — once the trade reaches +1R, trail at close - atr*mult,
                monotonic upward, mirroring position_mgmt.maybe_ratchet_stop
      floor   — as `atr`, but from +1R onward stop = max(atr trail, entry),
                which is the rule ratified 2026-09-03

    The trail is recomputed from a bar's CLOSE and applies from the NEXT bar,
    so no bar is exited on information it had not yet produced."""
    initial_stop = stop
    risk_per_share = entry - initial_stop
    reached_1r = False
    live_stop = stop

    for i in range(entry_i, len(df)):
        bar = df.iloc[i]
        o, h, l, c = (float(bar["open"]), float(bar["high"]),
                      float(bar["low"]), float(bar["close"]))

        if i > entry_i and o <= live_stop:
            return o, i, "gap_stop"
        if i > entry_i and o >= target:
            return o, i, "gap_target"
        if l <= live_stop:
            return live_stop, i, "stop"
        if h >= target:
            return target, i, "target"

        if mode == "static" or risk_per_share <= 0:
            continue

        # --- update the stop for the NEXT bar, from this bar's close ---
        if not reached_1r and (c - entry) / risk_per_share >= 1.0:
            reached_1r = True
        if not reached_1r:
            continue
        candidate = live_stop
        if atr is not None:
            try:
                atr_value = float(atr.iloc[i])
            except Exception:
                atr_value = float("nan")
            if not math.isnan(atr_value):
                candidate = c - atr_value * atr_mult
        if mode == "floor" and entry > candidate and entry < c:
            candidate = entry
        if candidate > live_stop and candidate < c:
            live_stop = candidate

    return None, None, "open"


def trades_from(df, symbol, strat_name, signals, target_r=2.0, mode="static",
                atr_mult=2.5):
    """One position per symbol at a time, like the live book."""
    atr = atr_series(df) if mode != "static" else None
    trades, busy_until = [], -1
    for sig in signals:
        i = sig["entry_i"]
        if i <= busy_until:
            continue
        entry, stop = sig["entry"], sig["stop"]
        target = entry + (entry - stop) * target_r
        exit_price, exit_i, reason = simulate_exit(
            df, i, entry, stop, target, mode=mode, atr=atr, atr_mult=atr_mult)
        if exit_price is None:
            break
        busy_until = exit_i
        trades.append({
            "symbol": symbol, "strategy": strat_name,
            "entry": entry, "stop": stop, "exit": float(exit_price),
            "r": (exit_price - entry) / (entry - stop),
            "pnl_usd": (exit_price - entry) * sig["qty"],
            "exit_reason": reason, "regime": sig["regime"],
            "bars_held": exit_i - i,
        })
    return trades


# ============================================================ reporting

def aggregate(trades):
    if not trades:
        return {"trades": 0, "win_rate": None, "avg_r": None,
                "expectancy_r": None, "profit_factor": None,
                "max_drawdown_r": None}
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [-r for r in rs if r <= 0]
    equity = peak = max_dd = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "trades": len(rs),
        "win_rate": round(100 * len(wins) / len(rs), 1),
        "avg_r": round(sum(rs) / len(rs), 3),
        "expectancy_r": round(sum(rs) / len(rs), 3),
        "profit_factor": (round(sum(wins) / sum(losses), 2)
                          if losses and sum(losses) > 0
                          else (float("inf") if wins else 0.0)),
        "max_drawdown_r": round(max_dd, 2),
    }


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return out


def fmt(stats):
    """(trades, win %, avg R, expectancy, PF) as display cells."""
    if not stats["trades"]:
        return ["0", "—", "—", "—", "—"]
    return [stats["trades"], f"{stats['win_rate']}%", stats["avg_r"],
            f"{stats['expectancy_r']:+.3f}", stats["profit_factor"]]


UPPER_BOUND = (
    "_Measured on SIGNALS, not on trades the desk took. Every signal counted "
    "here would still have had to clear the AI gatekeeper, whole-share sizing "
    "on a $2,000 cap, the max-positions cap and the daily-loss breaker, so "
    "these counts are an UPPER BOUND on what would have been realised._")


def append_agenda(title: str, lines, path: str = "BOARDROOM_AGENDA.md"):
    """Append one numbered item, continuing the file's existing numbering."""
    with open(path, encoding="utf-8") as f:
        body = f.read()
    used = [int(n) for n in
            __import__("re").findall(r"^## (\d+)\.", body, flags=8)]
    number = (max(used) + 1) if used else 1
    block = ["", "---", "", f"## {number}. {title}", ""] + list(lines) + [""]
    with open(path, "w", encoding="utf-8") as f:
        f.write(body.rstrip() + "\n".join(block) + "\n")
    return number
