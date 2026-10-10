"""
study_live_mfe.py — S17 (A). How far did each live trade run in our favour
before it exited, and how does that compare with the backtest signals?

    python study_live_mfe.py

LIVE. Every BUY/SELL pair in the journal (a SELL carries its BUY's
decision_id). Stop = the BUY's gatekeeper-context stop; CEO order-sheet
entries have no stop on record, so their R is n/a. MFE = the highest 5-minute
HIGH strictly after the entry bar and before the exit, in % and R; time to
MFE from the entry timestamp.

EXIT TIME. A "(synced)"/"(resolved)" SELL is stamped when sync ran, not when
it filled, and the old account's order history is no longer reachable with
the current keys. For those stop exits the exit time is estimated as the
first 5-minute bar at or below the exit price: after entry for an exit
below entry (its stop sat there from the start); on the stamp's own day (or
the session before) for a RAISED stop above entry, which was moved up only
shortly before it filled. Every other exit uses its journal stamp.

BACKTEST. The 6-month window's backtest signals (study_fade_exit), MFE
before the static bracket exit (structural stop or 3R target) on the same
5-minute bars: median, p25 and p75, in % and R.
"""

import json
import sqlite3
import sys

import pandas as pd

import study_common as sc
import study_fade_exit as fx


def live_pairs():
    c = sqlite3.connect(f"file:{fx.JOURNAL}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    buys = {r["decision_id"]: r for r in c.execute(
        "SELECT * FROM trades WHERE action='BUY'")}
    out = []
    for s in c.execute("SELECT * FROM trades WHERE action='SELL' ORDER BY id"):
        b = buys.get(s["decision_id"])
        if b is None:
            continue
        ctx = c.execute("SELECT context FROM decisions WHERE id=?",
                        (b["decision_id"],)).fetchone()
        stop = None
        if ctx and ctx["context"]:
            try:
                stop = json.loads(ctx["context"]).get("stop")
            except ValueError:
                stop = None
        out.append({"buy_id": b["id"], "sell_id": s["id"],
                    "ticker": b["ticker"], "setup": b["reason"],
                    "entry_ts": pd.Timestamp(b["timestamp"]).tz_localize(
                        "America/New_York"),
                    "exit_ts_journal": pd.Timestamp(s["timestamp"]).tz_localize(
                        "America/New_York"),
                    "entry": float(b["price"]), "exit": float(s["price"]),
                    "stop": float(stop) if stop is not None else None,
                    "reason": s["reason"], "pnl": float(s["pnl_usd"] or 0)})
    return out


def measure(p, df):
    after = df[df.index > p["entry_ts"]]          # bars that START after entry
    if after.empty:
        return None
    lagged = ("(synced)" in p["reason"] or "(resolved)" in p["reason"]) \
        and "Stop" in p["reason"]
    exit_ts = p["exit_ts_journal"]
    estimated = False
    if lagged:
        if p["exit"] >= p["entry"]:
            # A RAISED stop: it was moved up some time before it filled, so
            # the first crossing after entry is too early. "(resolved)" rows
            # are written in the same session and the sync ran the same
            # morning, so look for the first touch on the stamp's own day,
            # then the session before it.
            day = exit_ts.normalize()
            for lo in (day, day - pd.Timedelta(days=3)):
                cand = after[(after.index >= lo) & (after.index <= exit_ts)
                             & (after["low"] <= p["exit"])]
                if not cand.empty:
                    exit_ts, estimated = cand.index[0], True
                    break
        else:
            # Exit below entry: the stop sat at that price from entry.
            hit = after[(after["low"] <= p["exit"]) & (after.index <= exit_ts)]
            if not hit.empty:
                exit_ts, estimated = hit.index[0], True
    window = after[after.index < exit_ts]
    if window.empty:
        window = after.iloc[:1]
    hi_ts = window["high"].idxmax()
    hi = float(window["high"].max())
    risk = p["entry"] - p["stop"] if p["stop"] is not None else None
    return {
        "exit_ts": exit_ts, "estimated": estimated,
        "r_pct": 100 * risk / p["entry"] if risk else None,
        "mfe_pct": 100 * (hi / p["entry"] - 1),
        "mfe_r": (hi - p["entry"]) / risk if risk else None,
        "to_mfe": hi_ts - p["entry_ts"],
        "exit_r": (p["exit"] - p["entry"]) / risk if risk else None,
    }


def fmt_td(td):
    hours = td.total_seconds() / 3600
    return f"{hours / 24:.1f}d" if hours >= 24 else f"{hours:.1f}h"


def backtest_mfe():
    daily, regime = sc.load_bars()
    entries = fx.backtest_entries(daily, regime)
    m5 = fx.load_m5(sorted({e["symbol"] for e in entries}))
    trades = fx.replay(entries, m5, fx.steps_policy(()), False)
    return trades


def pct(values, q):
    s = pd.Series([v for v in values if v is not None])
    return f"{s.quantile(q):.2f}" if len(s) else "—"


def main():
    pairs = live_pairs()
    m5 = fx.load_m5(sorted({p["ticker"] for p in pairs}))
    lines = ["**Live trades: max favourable excursion before exit** "
             f"({len(pairs)} SELL rows; NOK and ORCL 08-13 are double fills, "
             f"so {len(pairs) - 2} position closes; no position is open on "
             "the current account). 5-minute bars, regular hours.", ""]
    rows = []
    for p in pairs:
        df = m5.get(p["ticker"])
        m = measure(p, df) if df is not None and not df.empty else None
        if m is None:
            rows.append([p["entry_ts"].strftime("%m-%d"), p["ticker"], p["setup"],
                         p["entry"], p["stop"] or "n/a", "—", "—", "—", "—",
                         f'{p["exit"]} ({p["reason"]})'])
            continue
        r_pct = f'{m["r_pct"]:.2f}%' if m["r_pct"] is not None else "n/a"
        mfe_r = f'{m["mfe_r"]:+.2f}R' if m["mfe_r"] is not None else "n/a"
        exit_r = f' {m["exit_r"]:+.2f}R' if m["exit_r"] is not None else ""
        rows.append([p["entry_ts"].strftime("%m-%d %H:%M"), p["ticker"],
                     p["setup"], p["entry"],
                     p["stop"] if p["stop"] is not None else "n/a", r_pct,
                     f'{m["mfe_pct"]:+.2f}%', mfe_r, fmt_td(m["to_mfe"]),
                     f'{p["exit"]}{exit_r}, {m["exit_ts"]:%m-%d %H:%M}'
                     + (" (est.)" if m["estimated"] else "")
                     + f' - {p["reason"]}'])
    lines += sc.table(["Entry (ET)", "Ticker", "Setup", "Entry", "Stop",
                       "R in %", "MFE %", "MFE R", "Time to MFE", "Exit"], rows)
    lines += ["", "_(est.) = exit time estimated from the bars (the SELL row "
              "was stamped at sync time). CEO order-sheet entries carry no "
              "stop on record, so R is n/a for them._", ""]

    trades = backtest_mfe()
    lines += ["**Backtest signals, last 6 months: MFE before the static "
              f"bracket exit** ({len(trades)} trades, 5-minute bars):", ""]
    brows = []
    for setup in (None,) + fx.SETUPS:
        ts = [t for t in trades if setup is None or t["setup"] == setup]
        brows.append([setup or "all", len(ts),
                      pct([t["mfe_pct"] for t in ts], 0.25),
                      pct([t["mfe_pct"] for t in ts], 0.50),
                      pct([t["mfe_pct"] for t in ts], 0.75),
                      pct([t["mfe_r"] for t in ts], 0.25),
                      pct([t["mfe_r"] for t in ts], 0.50),
                      pct([t["mfe_r"] for t in ts], 0.75),
                      pct([100 * (t["entry"] - t["stop"]) / t["entry"]
                           for t in ts], 0.50)])
    lines += sc.table(["Setup", "Trades", "MFE % p25", "MFE % median",
                       "MFE % p75", "MFE R p25", "MFE R median", "MFE R p75",
                       "Median R in %"], brows)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
