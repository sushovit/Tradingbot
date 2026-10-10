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
  g, h  pending definition (see VARIANTS).
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

# name -> (lock steps [(trigger_R, stop_R)], fade_exit)
VARIANTS = {
    "b BE at +1R": (((1.0, 0.0),), False),
    "m b + fade exit (EMA20, after +0.5R)": (((1.0, 0.0),), True),
}


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

def simulate_5m(df, start_pos, entry, stop, target, steps, fade):
    """Walk 5-minute bars from start_pos. Returns
    (exit_price, exit_ts, reason, mfe_r) or (None, None, "open", mfe_r)."""
    risk = entry - stop
    live_stop = stop
    mfe_r = 0.0
    fade_armed = False
    pending_fade = False
    o_ = df["open"].values
    h_ = df["high"].values
    l_ = df["low"].values
    c_ = df["close"].values
    e_ = df["ema20"].values
    idx = df.index
    for i in range(start_pos, len(df)):
        o, h, l, c = float(o_[i]), float(h_[i]), float(l_[i]), float(c_[i])
        if pending_fade:
            # The fade fired on the previous completed bar: out at this open,
            # unless the open itself is through the stop or the target.
            if o <= live_stop:
                return o, idx[i], "gap_stop", mfe_r
            return o, idx[i], "fade", mfe_r
        if i > start_pos and o <= live_stop:
            return o, idx[i], "gap_stop", mfe_r
        if i > start_pos and o >= target:
            return o, idx[i], "gap_target", max(mfe_r, (o - entry) / risk)
        mfe_r = max(mfe_r, (h - entry) / risk)
        if l <= live_stop:
            return live_stop, idx[i], "stop", mfe_r
        if h >= target:
            return target, idx[i], "target", mfe_r
        close_r = (c - entry) / risk
        for trigger_r, stop_r in steps:
            candidate = entry + stop_r * risk
            if close_r >= trigger_r and candidate > live_stop and candidate < c:
                live_stop = candidate
        if fade:
            if mfe_r >= 0.5:
                fade_armed = True
            if fade_armed and not math.isnan(e_[i]) and c < e_[i]:
                pending_fade = True
    return None, None, "open", mfe_r


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


def replay(entries, m5, steps, fade):
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
        target = e["entry"] + (e["entry"] - e["stop"]) * TARGET_R
        exit_price, exit_ts, reason, mfe_r = simulate_5m(
            df, pos, e["entry"], e["stop"], target, steps, fade)
        if exit_price is None:
            replay.still_open += 1      # open at window end: not counted
            busy[key] = df.index[-1]
            continue
        busy[key] = exit_ts
        trades.append({**e, "exit": exit_price, "exit_ts": exit_ts,
                       "exit_reason": reason, "mfe_r": mfe_r,
                       "r": (exit_price - e["entry"]) / (e["entry"] - e["stop"])})
    return trades


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
    results, still_open = {}, {}
    for label, (steps, fade) in VARIANTS.items():
        results[label] = replay(entries + live, m5, steps, fade)
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
                               pct_loss_after(ts, 0.5)])
    lines += sc.table(["Entries", "Setup", "Variant", "Trades", "Win %",
                       "Avg R", "Expectancy", "PF", "MaxDD (R)",
                       "Hit +1.5R, closed at a loss",
                       "Hit +0.5R, closed at a loss"], rows)
    lines += ["", "**Exit mix (all entries):**", ""]
    mix = []
    for label in VARIANTS:
        for setup in SETUPS:
            ts = [t for t in results[label] if t["setup"] == setup]
            n = lambda k: sum(1 for t in ts if t["exit_reason"] == k)
            mix.append([setup, label, n("target") + n("gap_target"),
                        n("stop") + n("gap_stop"), n("fade")])
    lines += sc.table(["Setup", "Variant", "target", "stop", "fade"], mix)
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
