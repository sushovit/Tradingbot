"""
study_capital_cap.py — W3(d). What would a $5,000 capital cap have bought?

    python study_capital_cap.py [--append]

Two populations, because the one the order names is far too small to decide
anything on:

  JOURNAL REPLAY (as ordered) — every journaled size_zero /
  price_too_high_for_account row, re-sized under capital_cap_usd 5000, same
  shape as regime_audit.py.

  BACKTEST REPLAY (added) — the same question over 3y of signals, where the
  population is thousands of signals rather than a handful of rows. This is
  the one that can actually inform the boardroom.

The journal population is tiny for a structural reason worth stating: a
size_zero row is only written when a signal has already cleared every other
gate and then fails whole-share sizing. Enriched details (stop distance and
risk budget) only began on 2026-09-02, so older rows carry entry and equity
alone and their stop must be reconstructed from bars.
"""

import argparse
import json
import re
import sqlite3
import sys

import risk
import study_common as sc

CURRENT_CAP = 2000.0
PROPOSED_CAP = 5000.0
RISK_PCT = 1.0
POSITION_CAP_PCT = 0.25
REASONS = ("size_zero", "price_too_high_for_account")


# ============================================================ journal side

def journal_rows(db_file: str = "journal.db"):
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT timestamp, ticker, setup_name, "
        "json_extract(verdict,'$.rejection_reason') AS reason, "
        "json_extract(context,'$.details') AS details "
        "FROM decisions WHERE source='rules' "
        "AND json_extract(verdict,'$.rejection_reason') IN (?,?) "
        "ORDER BY timestamp", REASONS).fetchall()
    conn.close()
    out = []
    for r in rows:
        details = r["details"] or ""
        out.append({
            "date": str(r["timestamp"])[:10],
            "month": str(r["timestamp"])[:7],
            "ticker": r["ticker"], "setup": r["setup_name"],
            "reason": r["reason"],
            "entry": _num(details, "entry"),
            "equity": _num(details, "equity"),
            "stop_distance": _num(details, "stop_distance_usd"),
        })
    return out


def _num(text: str, key: str):
    m = re.search(rf"{key}=([0-9]*\.?[0-9]+)", text or "")
    return float(m.group(1)) if m else None


def reconstruct_stop(row, bars):
    """Recover the stop distance for a pre-2026-09-02 row, whose details
    carry only entry and equity. Replays the setup's detector on that day's
    bars. Returns None when the symbol has no cached history."""
    df = bars.get(row["ticker"])
    if df is None or df.empty or not row["entry"]:
        return None
    from strategies import REGISTRY
    cls = REGISTRY.get(row["setup"])
    if cls is None:
        return None
    dates = df.index.astype(str).str[:10]
    idx = [i for i, d in enumerate(dates) if d <= row["date"]]
    if len(idx) < 60:
        return None
    window = df.iloc[max(0, idx[-1] - 59):idx[-1] + 1]
    try:
        result = cls().detect(window, {"ticker": row["ticker"],
                                       "risk_profile": sc.study_profile(),
                                       "config": sc.study_config()})
    except Exception:
        return None
    stop = getattr(result, "stop", None)
    if stop is None:
        return None
    distance = row["entry"] - float(stop)
    return distance if distance > 0 else None


def sizeable(entry, stop_distance, cap):
    """Whole shares under `cap`, using the live sizing rules."""
    if not entry or not stop_distance or stop_distance <= 0:
        return None
    stop = entry - stop_distance
    return risk.position_size(cap, RISK_PCT, entry, stop,
                              position_cap_pct=POSITION_CAP_PCT)


def replay_journal(bars):
    rows = journal_rows()
    for row in rows:
        if row["stop_distance"] is None:
            row["stop_distance"] = reconstruct_stop(row, bars)
            row["reconstructed"] = row["stop_distance"] is not None
        else:
            row["reconstructed"] = False
        row["qty_2000"] = sizeable(row["entry"], row["stop_distance"],
                                   CURRENT_CAP)
        row["qty_5000"] = sizeable(row["entry"], row["stop_distance"],
                                   PROPOSED_CAP)
    return rows


# ============================================================ backtest side

def raw_signals(symbol, df, setup, regime, profile, config):
    """Every detector fire with its entry/stop geometry, BEFORE the sizing
    filter.

    backtest.collect_signals cannot be used here: it applies
    `if qty < 1: continue` internally, so the signals this study is trying to
    count have already been discarded by the time it returns. (Measured:
    using it reported 0% unlocked for every setup, which is impossible — the
    live desk demonstrably fails to size CRM-shaped trades.) This replicates
    its next-bar-open entry and stop conventions exactly, and stops short of
    the sizing rejection."""
    import backtest
    from strategies import REGISTRY, Signal
    strat = REGISTRY[setup]()
    context = {"ticker": symbol, "risk_profile": dict(profile),
               "config": dict(config)}
    out = []
    for i in range(backtest.WARMUP_BARS, len(df)):
        window = df.iloc[max(0, i - backtest.DETECT_WINDOW + 1):i + 1]
        try:
            result = strat.detect(window, context)
        except Exception:
            continue
        if not isinstance(result, Signal):
            continue
        entry = float(df["open"].iloc[i])
        stop_dist = result.entry - result.stop
        if setup == "trend_continuation":
            stop = entry - stop_dist        # ATR distance measured from fill
        else:
            stop = result.stop              # absolute bar-low level
        if stop >= entry:
            continue
        out.append({"entry": entry, "stop": stop,
                    "month": str(df.index[i])[:7]})
    return out


def replay_backtest(bars, regime, setups=("trend_continuation",
                                          "momentum_continuation",
                                          "mean_reversion_reclaim")):
    """For every detector fire in 3y: sizeable at $2,000, and at $5,000?"""
    profile, config = sc.study_profile(), sc.study_config()
    counts, by_month = {}, {}
    for setup in setups:
        blocked = both = only_5k = 0
        for symbol, df in bars.items():
            for sig in raw_signals(symbol, df, setup, regime, profile,
                                   config):
                distance = sig["entry"] - sig["stop"]
                q2 = sizeable(sig["entry"], distance, CURRENT_CAP) or 0
                q5 = sizeable(sig["entry"], distance, PROPOSED_CAP) or 0
                if q2 >= 1:
                    both += 1
                elif q5 >= 1:
                    only_5k += 1
                    key = (setup, sig["month"])
                    by_month[key] = by_month.get(key, 0) + 1
                else:
                    blocked += 1
        counts[setup] = {"sizeable_at_2000": both,
                         "unlocked_by_5000": only_5k,
                         "still_blocked": blocked}
    return counts, by_month


# ============================================================ report

def render(rows, counts, years, by_month=None):
    total = len(rows)
    unlocked = [r for r in rows
                if (r["qty_2000"] or 0) < 1 and (r["qty_5000"] or 0) >= 1]
    indeterminate = [r for r in rows if r["stop_distance"] is None]

    lines = [
        f"**Question.** How much does the $2,000 capital cap cost in entries, "
        f"and would $5,000 recover them?",
        "",
        f"### Journal replay (as ordered) — n = {total}",
        "",
    ]
    if total:
        table_rows = []
        for r in rows:
            def cell(q):
                return "—" if q is None else ("**yes**" if q >= 1 else "no")
            table_rows.append([
                r["date"], r["ticker"], r["setup"], r["reason"],
                f"{r['entry']:.2f}" if r["entry"] else "—",
                f"{r['stop_distance']:.2f}" if r["stop_distance"] else "—",
                cell(r["qty_2000"]), cell(r["qty_5000"]),
                "reconstructed" if r.get("reconstructed") else
                ("journaled" if r["stop_distance"] else "unavailable")])
        lines += sc.table(["Date", "Ticker", "Setup", "Reason", "Entry",
                           "Stop dist", "Sizeable @ $2k", "Sizeable @ $5k",
                           "Stop source"], table_rows)
    else:
        lines.append("_No such rows journaled._")
    lines += [
        "",
        f"Of {total} journaled rows, **{len(unlocked)}** would have become "
        f"sizeable at $5,000"
        + (f"; {len(indeterminate)} are indeterminate because the stop could "
           f"not be recovered." if indeterminate else "."),
        "",
        "**This population is too small to decide on, and the reason is "
        "structural.** A `size_zero` row is only written when a signal has "
        "already cleared every other gate and then fails whole-share sizing, "
        "so the journal sees only the survivors of a narrow funnel. Enriched "
        "details (stop distance, risk budget) began on 2026-09-02, so older "
        "rows carry entry and equity alone and their stop had to be "
        "reconstructed by replaying the detector on that day's bars — "
        "possible only for symbols with cached history.",
        "",
        f"### Backtest replay ({years}y, all signals) — the usable population",
        "",
    ]
    rows2 = []
    for setup, c in counts.items():
        total_sig = sum(c.values())
        pct = (100.0 * c["unlocked_by_5000"] / total_sig) if total_sig else 0
        rows2.append([setup, total_sig, c["sizeable_at_2000"],
                      c["unlocked_by_5000"], c["still_blocked"],
                      f"{pct:.1f}%"])
    lines += sc.table(["Setup", "Signals", "Sizeable @ $2k",
                       "Unlocked by $5k", "Still blocked",
                       "% unlocked"], rows2)
    if by_month:
        lines += ["", "**Unlocked entries by month** (the $5k-only column, "
                  "spread over time):", ""]
        months = sorted({m for _, m in by_month})
        setups = sorted({s for s, _ in by_month})
        lines += sc.table(["Month"] + setups,
                          [[m] + [by_month.get((s, m), 0) for s in setups]
                           for m in months])
    lines += [
        "",
        "**Reading.** The `% unlocked` column is what raising the cap buys in "
        "entry COUNT. It says nothing about whether those entries are "
        "profitable — they are the trades the desk currently cannot afford, "
        "which skew toward wider stops and higher-priced names, not a random "
        "sample of the edge. Pair this with the expectancy studies before "
        "reading it as upside.",
        "",
        "**Method.** Sizing uses the live rules (`risk.position_size`, 1% "
        "risk, 25% notional cap, whole shares). `bot_config.json` was not "
        "modified; the cap is passed as a parameter. Reproduce with "
        "`python study_capital_cap.py`.",
        "",
        sc.UPPER_BOUND,
    ]
    return lines


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--append", action="store_true")
    p.add_argument("--years", type=int, default=sc.YEARS)
    args = p.parse_args()

    bars, regime = sc.load_bars(years=args.years)
    # The journal's tickers need not be in the cached watchlist (SPCX, CRM
    # were not), so pull their history too before trying to recover stops.
    extra = sorted({r["ticker"] for r in journal_rows()} - set(bars))
    if extra:
        try:
            more, _ = sc.load_bars(symbols=extra, years=args.years)
            bars = dict(bars, **more)
        except Exception as e:
            print(f"(could not fetch {extra}: {e})")
    rows = replay_journal(bars)
    counts, by_month = replay_backtest(bars, regime)
    lines = render(rows, counts, args.years, by_month)
    print("\n".join(lines))
    if args.append:
        n = sc.append_agenda(
            "Study: capital cap — $2,000 vs $5,000 (W3d)", lines)
        print(f"\nAppended to BOARDROOM_AGENDA.md as item {n}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
