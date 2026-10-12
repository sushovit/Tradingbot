"""
study_fade_exit.py — S17 (m). A 5-minute replay of the daily setups over the
last six months, so the "fade exit" can be compared like-for-like with the
profit lock the desk actually runs.

    python study_fade_exit.py

ENTRIES. (1) Every backtest signal for momentum_continuation and
mean_reversion_reclaim whose entry day falls in the window: entry at the
entry day's open (as backtest.collect_signals fills), replayed from that
session's first regular-hours bar. (2) Every journaled live bot entry in the
window whose gatekeeper context carries a stop: entry at the journaled fill,
replayed from the bar AFTER the one containing the fill. Target 3R for all.

BARS. Alpaca IEX 5-minute bars, regular hours only (09:30-16:00 ET), cached
in data/m5_<SYM>.csv. A bar opening through a level fills at its open; a
bar spanning stop and target hits the stop first - simulate_exit's rules.

VARIANTS (all keep the 3R target):
  b   BE at +1R: once a completed bar CLOSES at +1R, stop -> entry from the
      next bar. This is the live S16 rule.
  m   b, plus the fade exit: once the trade has been >= +0.5R (any bar HIGH),
      exit at the first completed close below the 5-minute EMA20 - filled at
      the NEXT bar's open, the first price a live worker could act on.
  j   BE at +1R, then trail at the highest 5-minute CLOSE since entry minus
      0.5R; no fixed target (S17).
  g, h  pending definition.
"""

import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

import study_common as sc

TARGET_R = 3.0
SETUPS = ("momentum_continuation", "mean_reversion_reclaim")
WINDOW_END = pd.Timestamp("2026-10-10", tz="America/New_York")
WINDOW_START = WINDOW_END - pd.DateOffset(months=6)
M5_DIR = "data"
JOURNAL = os.environ.get("S17_JOURNAL", "../TradingBot/journal.db")

# name -> (stop policy, fade_exit). Policies are defined below simulate_5m;
# VARIANTS is filled in after them.
VARIANTS = {}


# ============================================================ 5-minute bars

def load_m5(symbols, refresh=False):
    """{symbol: RTH 5-minute DataFrame indexed in America/New_York}."""
    from dotenv import load_dotenv
    load_dotenv(".env")
    os.makedirs(M5_DIR, exist_ok=True)
    out, missing = {}, []
    for s in symbols:
        path = os.path.join(M5_DIR, f"m5_{s}.csv")
        if os.path.exists(path) and not refresh:
            df = pd.read_csv(path, index_col=0)
            df.index = pd.to_datetime(df.index, utc=True).tz_convert(
                "America/New_York")
            out[s] = df
        else:
            missing.append(s)
    if missing:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        from broker import Broker
        data = Broker().data
        start = (WINDOW_START - pd.Timedelta(days=10)).tz_convert("UTC")
        for i in range(0, len(missing), 10):
            batch = missing[i:i + 10]
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                start=start.to_pydatetime(),
                end=WINDOW_END.tz_convert("UTC").to_pydatetime(),
                feed=DataFeed.IEX)
            df_all = data.get_stock_bars(req).df
            for s in batch:
                if df_all.empty or s not in df_all.index.get_level_values(0):
                    out[s] = pd.DataFrame()
                    continue
                df = df_all.xs(s, level=0)[["open", "high", "low", "close",
                                            "volume"]].copy()
                df.index = pd.to_datetime(df.index, utc=True).tz_convert(
                    "America/New_York")
                t = df.index.time
                df = df[(t >= pd.Timestamp("09:30").time())
                        & (t < pd.Timestamp("16:00").time())]
                df.to_csv(os.path.join(M5_DIR, f"m5_{s}.csv"))
                out[s] = df
    for s, df in out.items():
        if not df.empty:
            df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    return out


# ============================================================ replay

def _variants():
    entry_ = lambda entry, risk: entry
    return {
        "b BE at +1R": (steps_policy(((1.0, 0.0),)), False),
        "m b + fade exit (EMA20, after +0.5R)": (steps_policy(((1.0, 0.0),)), True),
        "n +1.5% -> BE, trail high-1.5%": (pct_policy([(1.5, entry_)], 1.5), False),
        "o +2% -> BE, trail high-2%": (pct_policy([(2.0, entry_)], 2.0), False),
        "p +1% -> -0.5R, +2% -> BE, trail high-2%": (pct_policy(
            [(1.0, lambda entry, risk: entry - 0.5 * risk), (2.0, entry_)], 2.0),
            False),
        "q +1xATR -> BE, trail high-1xATR": (atr_policy(1.0), False),
        J_LABEL: (close_trail_policy(1.0, 0.5), False),
    }


J_LABEL = "j BE at +1R, trail high close-0.5R, no target"
NO_TARGET = {J_LABEL}


def close_trail_policy(be_r, trail_r):
    """j: once a completed close reaches +be_r R, stop -> entry; from then on
    also trail at the highest CLOSE since entry minus trail_r R."""
    def policy(entry, risk, c, hh, atr, mem):
        mem["hc"] = max(mem.get("hc", entry), c)
        if (c - entry) / risk >= be_r:
            mem["armed"] = True
        if mem.get("armed"):
            return max(entry, mem["hc"] - trail_r * risk)
        return None
    return policy


def steps_policy(steps):
    """R steps [(trigger_R, stop_R)] on a completed close (b, k, l...)."""
    def policy(entry, risk, c, hh, atr, mem):
        best = None
        for trigger_r, stop_r in steps:
            if (c - entry) / risk >= trigger_r:
                best = entry + stop_r * risk
        return best
    return policy


def pct_policy(stages, trail_pct):
    """Percent stages [(trigger_pct, stop_fn(entry, risk))] decided on a
    completed close; once the LAST stage has triggered, also trail at the
    highest price since entry minus trail_pct (n, o, p)."""
    def policy(entry, risk, c, hh, atr, mem):
        best = None
        gain = (c / entry - 1) * 100
        for n, (trigger_pct, stop_fn) in enumerate(stages):
            if gain >= trigger_pct:
                mem["stage"] = max(mem.get("stage", -1), n)
        if mem.get("stage", -1) >= 0:
            best = stages[mem["stage"]][1](entry, risk)
        if mem.get("stage", -1) == len(stages) - 1:
            best = max(best, hh * (1 - trail_pct / 100))
        return best
    return policy


def atr_policy(mult):
    """q: at +mult x daily ATR (completed close) stop -> entry; then trail at
    the highest price since entry minus mult x daily ATR."""
    def policy(entry, risk, c, hh, atr, mem):
        if atr is None or math.isnan(atr):
            return None
        if c >= entry + mult * atr:
            mem["armed"] = True
        if mem.get("armed"):
            return max(entry, hh - mult * atr)
        return None
    return policy


def simulate_5m(df, start_pos, entry, stop, target, policy, fade, atr=None):
    """Walk 5-minute bars from start_pos. The policy proposes a stop from
    each COMPLETED bar (close, highest high since entry, daily ATR); it
    applies from the next bar, never lowers, and is never placed at or above
    that close. Returns (exit_price, exit_ts, reason, mfe_r, mfe_pct), with
    exit_price None while the trade is still open at the data's end."""
    if not callable(policy):
        policy = steps_policy(policy)
    risk = entry - stop
    live_stop = stop
    mfe_r = mfe_pct = 0.0
    hh = entry
    mem = {}
    fade_armed = pending_fade = False
    o_, h_, l_, c_, e_ = (df[k].values for k in
                          ("open", "high", "low", "close", "ema20"))
    idx = df.index
    for i in range(start_pos, len(df)):
        o, h, l, c = float(o_[i]), float(h_[i]), float(l_[i]), float(c_[i])
        if pending_fade:
            if o <= live_stop:
                return o, idx[i], "gap_stop", mfe_r, mfe_pct
            return o, idx[i], "fade", mfe_r, mfe_pct
        if i > start_pos and o <= live_stop:
            return o, idx[i], "gap_stop", mfe_r, mfe_pct
        if i > start_pos and o >= target:
            return (o, idx[i], "gap_target", max(mfe_r, (o - entry) / risk),
                    max(mfe_pct, (o / entry - 1) * 100))
        hh = max(hh, h)
        mfe_r = max(mfe_r, (h - entry) / risk)
        mfe_pct = max(mfe_pct, (h / entry - 1) * 100)
        if l <= live_stop:
            return live_stop, idx[i], "stop", mfe_r, mfe_pct
        if h >= target:
            return target, idx[i], "target", mfe_r, mfe_pct
        candidate = policy(entry, risk, c, hh, atr, mem)
        if candidate is not None and live_stop < candidate < c:
            live_stop = candidate
        if fade:
            if mfe_r >= 0.5:
                fade_armed = True
            if fade_armed and not math.isnan(e_[i]) and c < e_[i]:
                pending_fade = True
    return None, None, "open", mfe_r, mfe_pct


# ============================================================ entries

def backtest_entries(daily_bars, regime):
    import backtest
    profile, config = sc.study_profile(), sc.study_config()
    entries = []
    for setup in SETUPS:
        for symbol, df in daily_bars.items():
            if symbol == "SPY":
                continue
            sigs, _ = backtest.collect_signals(symbol, df, setup, regime,
                                               profile, config)
            for sig in sigs:
                day = pd.Timestamp(df.index[sig["entry_i"]])
                day = day.tz_localize("America/New_York") if day.tzinfo is None \
                    else day.tz_convert("America/New_York")
                if not (WINDOW_START <= day < WINDOW_END):
                    continue
                entries.append({"source": "backtest", "setup": setup,
                                "symbol": symbol, "day": day.normalize(),
                                "entry": float(sig["entry"]),
                                "stop": float(sig["stop"])})
    return entries


def live_entries():
    """Journaled bot BUYs in the window whose gatekeeper context has a stop."""
    if not os.path.exists(JOURNAL):
        return [], 0
    c = sqlite3.connect(f"file:{JOURNAL}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT t.id, t.timestamp, t.ticker, t.price, t.reason, d.context "
        "FROM trades t LEFT JOIN decisions d ON d.id = t.decision_id "
        "WHERE t.action='BUY'").fetchall()
    out, skipped = [], 0
    for r in rows:
        ts = pd.Timestamp(r["timestamp"]).tz_localize("America/New_York")
        if not (WINDOW_START <= ts < WINDOW_END):
            continue
        stop = None
        if r["context"]:
            try:
                stop = json.loads(r["context"]).get("stop")
            except ValueError:
                stop = None
        if stop is None or r["reason"] not in SETUPS \
                or float(stop) >= float(r["price"]):
            skipped += 1
            continue
        out.append({"source": "live", "setup": r["reason"],
                    "symbol": r["ticker"], "ts": ts,
                    "entry": float(r["price"]), "stop": float(stop),
                    "trade_id": r["id"]})
    return out, skipped


def start_position(df, e):
    if e["source"] == "backtest":
        day_rows = df.index.normalize() == e["day"]
        if not day_rows.any():
            return None
        return int(day_rows.argmax())
    pos = int(df.index.searchsorted(e["ts"], side="right"))
    return pos if pos < len(df) else None


def replay(entries, m5, policy, fade, atr_of=None, target_r=TARGET_R):
    """One position per (source, symbol) at a time, like the live book."""
    trades = []
    busy = {}
    replay.still_open = 0
    for e in sorted(entries, key=lambda x: x.get("day", x.get("ts"))):
        df = m5.get(e["symbol"])
        if df is None or df.empty:
            continue
        pos = start_position(df, e)
        if pos is None:
            continue
        key = (e["source"], e["symbol"])
        if key in busy and df.index[pos] <= busy[key]:
            continue
        target = (float("inf") if target_r is None
                  else e["entry"] + (e["entry"] - e["stop"]) * target_r)
        atr = atr_of(e) if atr_of else None
        exit_price, exit_ts, reason, mfe_r, mfe_pct = simulate_5m(
            df, pos, e["entry"], e["stop"], target, policy, fade, atr)
        if exit_price is None:
            replay.still_open += 1      # open at window end: not counted
            busy[key] = df.index[-1]
            continue
        busy[key] = exit_ts
        trades.append({**e, "exit": exit_price, "exit_ts": exit_ts,
                       "exit_reason": reason, "mfe_r": mfe_r,
                       "mfe_pct": mfe_pct,
                       "r": (exit_price - e["entry"]) / (e["entry"] - e["stop"])})
    return trades


def daily_atr_lookup(daily):
    """entry -> daily ATR(14) as of the last COMPLETED day before entry."""
    atrs = {s: sc.atr_series(df) for s, df in daily.items()}
    def atr_of(e):
        series = atrs.get(e["symbol"])
        if series is None:
            return None
        day = e.get("day")
        if day is None:
            day = e["ts"].normalize()
        idx = pd.DatetimeIndex(series.index)
        idx = idx.tz_localize("America/New_York") if idx.tz is None \
            else idx.tz_convert("America/New_York")
        prior = series[idx < day].dropna()
        return float(prior.iloc[-1]) if len(prior) else None
    return atr_of


def pct_loss_after_pct(trades, pct=1.0):
    if not trades:
        return "—"
    n = sum(1 for t in trades if t["mfe_pct"] >= pct and t["r"] < 0)
    return f"{100 * n / len(trades):.1f}%"


def pct_loss_after(trades, reached):
    if not trades:
        return "—"
    n = sum(1 for t in trades if t["mfe_r"] >= reached and t["r"] < 0)
    return f"{100 * n / len(trades):.1f}%"


def run():
    daily, regime = sc.load_bars()
    entries = backtest_entries(daily, regime)
    live, skipped = live_entries()
    symbols = sorted({e["symbol"] for e in entries + live})
    m5 = load_m5(symbols)
    atr_of = daily_atr_lookup(daily)
    VARIANTS.update(_variants())
    results, still_open = {}, {}
    for label, (policy, fade) in VARIANTS.items():
        results[label] = replay(entries + live, m5, policy, fade, atr_of,
                                target_r=None if label in NO_TARGET else TARGET_R)
        still_open[label] = replay.still_open
    run.still_open = still_open
    return results, len(entries), len(live), skipped, symbols


def render(results, n_bt, n_live, skipped, symbols):
    lines = [
        f"**5-minute replay, {WINDOW_START:%Y-%m-%d} to {WINDOW_END:%Y-%m-%d}.** "
        f"{n_bt} backtest signals and {n_live} journaled live entries "
        f"({skipped} live BUYs skipped: no stop in their decision context, "
        f"or not a daily setup); {len(symbols)} symbols; Alpaca IEX 5-min "
        f"bars, regular hours; target {TARGET_R:g}R.",
        "",
    ]
    rows = []
    for scope in ("all", "backtest", "live"):
        for label in VARIANTS:
            trades = [t for t in results[label]
                      if scope == "all" or t["source"] == scope]
            for setup in SETUPS:
                ts = [t for t in trades if t["setup"] == setup]
                st = sc.aggregate(ts)
                rows.append([scope, setup, label] + sc.fmt(st)
                            + [st["max_drawdown_r"] if st["trades"] else "—",
                               pct_loss_after(ts, 1.5),
                               pct_loss_after(ts, 0.5),
                               pct_loss_after_pct(ts, 1.0)])
    lines += sc.table(["Entries", "Setup", "Variant", "Trades", "Win %",
                       "Avg R", "Expectancy", "PF", "MaxDD (R)",
                       "Hit +1.5R, closed at a loss",
                       "Hit +0.5R, closed at a loss",
                       "Was >= +1% up, closed at a loss"], rows)
    lines += ["", "**Exit mix (all entries):**", ""]
    mix = []
    for label in VARIANTS:
        for setup in SETUPS:
            ts = [t for t in results[label] if t["setup"] == setup]
            n = lambda k: sum(1 for t in ts if t["exit_reason"] == k)
            locked = sum(1 for t in ts if t["exit_reason"] in ("stop", "gap_stop")
                         and t["r"] >= 0)
            mix.append([setup, label, n("target") + n("gap_target"),
                        n("stop") + n("gap_stop") - locked, locked, n("fade")])
    lines += sc.table(["Setup", "Variant", "target", "stop at a loss",
                       "stop at >= 0R (locked/trailed)", "fade"], mix)
    lines += ["", "Trades still open at the window end (excluded above): "
              + "; ".join(f"{k}: {v}" for k, v in run.still_open.items()) + "."]
    return lines


def main():
    argparse.ArgumentParser().parse_args()
    results, n_bt, n_live, skipped, symbols = run()
    print("\n".join(render(results, n_bt, n_live, skipped, symbols)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
