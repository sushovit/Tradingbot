"""
study_adx_band.py — W3(a). trend_continuation expectancy by ADX band.

    python study_adx_band.py [--append]
    python study_adx_band.py --s33 [--fetch] [--append]     # S33, see below

Question: is a signal at ADX 25-30 worth taking, or does the edge live only
at ADX >= 30?

METHOD NOTE THAT MATTERS. The live desk runs adx_threshold 30 and the
backtest base runs 28, so no signal below 28 has ever been generated — the
25-30 band does not exist in the live population and cannot be counted from
it. This study lowers its OWN threshold to 25 (a study-local risk profile;
bot_config.json is untouched) to admit the wider population, then buckets by
the ADX actually observed on each signal bar.
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime, time as dtime, timedelta

import study_common as sc

STUDY_ADX_FLOOR = 25
TARGET_R = 3.0          # the live rr_ratio for this setup
BANDS = (("ADX 25-30", 25.0, 30.0), ("ADX >= 30", 30.0, float("inf")))


def run(years: int = sc.YEARS):
    bars, regime = sc.load_bars(years=years)
    profile = sc.study_profile(adx_threshold=STUDY_ADX_FLOOR)
    config = sc.study_config()

    signals = sc.collect_with_features(
        bars, regime, "trend_continuation", profile, config,
        feature_fns={"adx": sc.adx_at})

    by_symbol = {}
    for sig in signals:
        by_symbol.setdefault(sig["symbol"], []).append(sig)

    trades = []
    for symbol, sigs in by_symbol.items():
        sigs.sort(key=lambda s: s["entry_i"])
        made = sc.trades_from(bars[symbol], symbol, "trend_continuation",
                              sigs, target_r=TARGET_R, mode="static")
        # carry the signal's ADX onto its trade
        for trade, sig in zip(made, [s for s in sigs][:len(made)]):
            trade["adx"] = sig.get("adx")
        trades.extend(made)

    buckets = {name: [] for name, _, _ in BANDS}
    unbanded = 0
    for trade in trades:
        adx = trade.get("adx")
        if adx is None:
            unbanded += 1
            continue
        for name, low, high in BANDS:
            if low <= adx < high:
                buckets[name].append(trade)
                break
    return buckets, len(trades), unbanded


def render(buckets, total, unbanded, years):
    rows = []
    for name in buckets:
        stats = sc.aggregate(buckets[name])
        rows.append([name] + sc.fmt(stats)
                    + [stats["max_drawdown_r"] if stats["trades"] else "—"])
    lines = [
        f"**Question.** Does trend_continuation's edge exist at ADX 25-30, or "
        f"only at ADX >= 30? {years}y daily, {TARGET_R}R target, "
        f"{total} trades from the core watchlist.",
        "",
    ]
    lines += sc.table(
        ["ADX band", "Trades", "Win %", "Avg R", "Expectancy", "PF",
         "MaxDD (R)"], rows)
    lines += [
        "",
        f"**Method.** The live desk runs adx_threshold 30 and the backtest "
        f"base runs 28, so signals below 28 have never been generated and the "
        f"25-30 band cannot be read from live history. This study lowered its "
        f"own threshold to {STUDY_ADX_FLOOR} to admit the wider population, "
        f"then bucketed by the ADX observed on each signal bar. "
        f"`bot_config.json` was not modified. Reproduce with "
        f"`python study_adx_band.py`.",
    ]
    if unbanded:
        lines.append(f"{unbanded} trade(s) had no computable ADX and are "
                     f"excluded.")
    lines += ["", sc.UPPER_BOUND]
    return lines


# =========================================================== S33 (2026-10-19)
# CEO order S33 item 1. Study only - bot_config.json and the live rule are
# untouched. trend_continuation is the only setup with an ADX gate; it runs
# INTRADAY on 5-minute IEX bars (config interval 5), so the replay is on a
# year of 5-minute bars, not daily ones.
#
# Signals: the live filter chain of strategies/trend_continuation.py, in its
# order - EMA9/EMA21 golden cross on the last completed bar, ADX14 >=
# threshold, RSI14 >= rsi_threshold (55), confirmation (EMA9 > EMA21 on the
# entry bar), volume >= its 20-bar EMA - then the worker's gates: SPY above
# its 20-day EMA (last completed session; Rule #5) and primary hours (entry
# 09:30-11:30 or 14:00-15:00 ET). Entry at the close of the bar after the
# cross (the live entry is the in-progress bar's price; its close is the
# nearest observable stand-in). One position per symbol at a time.
#
# Exits: the CURRENT intraday rule. Stop = entry - 2.0 x ATR14 (5m), target
# = entry + 3R, capped by S28 at fill + 3.0 x daily ATR14. The initial stop
# stands until +1R (on a 5-minute close); from then on the stop trails at
# close - 2.5 x ATR14 (5m), floored at entry (ratified breakeven floor),
# raised only, always below the close. No end-of-day exit (intraday
# positions are held until stop or target, as live). A bar opening through a
# level fills at its open; a bar spanning stop and target takes the stop.
#
# Thresholds: current (30), -5 (25), -10 (20), and regime-dependent: SPY's
# daily ADX14 (last completed session) >= 25 -> 20, 20-25 -> 25, < 20 -> 30
# (a strong market trend admits weaker single-name trends).

S33_START, S33_END = "2025-10-14", "2026-10-19"
S33_WARMUP_DAYS = 60
import bars_store                       # CEO 2026-10-25: the permanent store
S33_CACHE = bars_store.BARS_DIR
RTH = (dtime(9, 30), dtime(16, 0))
ENTRY_WINDOWS = ((dtime(9, 30), dtime(11, 30)), (dtime(14, 0), dtime(15, 0)))
EXPECTANCY_BAR = 0.15


def s33_settings(config=None):
    if config is None:
        with open("bot_config.json", encoding="utf-8") as f:
            config = json.load(f)
    rp = config["risk_profiles"]["Moderate"]
    return {"adx": float(rp["adx_threshold"]), "atr_mult": float(rp["atr_multiplier"]),
            "rr": float(rp["rr_ratio"]), "trail_atr": float(rp["trailing_stop_value"]),
            "rsi": float(config.get("rsi_threshold", 55)) if config.get("use_rsi_filter", True) else None,
            "confirm": bool(config.get("use_confirmation_candle", True)),
            "volume": bool(rp.get("use_volume_filter", True)),
            "cap_atr": float((config.get("exits") or {}).get("target_cap_atr", 3.0))}


def threshold_rules(current: float):
    def regime(spy_adx):
        if spy_adx is None:
            return current
        return current - 10 if spy_adx >= 25 else current - 5 if spy_adx >= 20 else current
    return [(f"current ({current:g})", lambda _a, c=current: c),
            (f"-5 ({current - 5:g})", lambda _a, c=current: c - 5),
            (f"-10 ({current - 10:g})", lambda _a, c=current: c - 10),
            ("regime (SPY ADX)", regime)]


def fetch_m5_year(symbols, start=S33_START, end=S33_END, cache=S33_CACHE):
    """5-minute IEX bars (the live feed), cached per symbol. The ONLY network
    use of this study, and only with --fetch: never during market hours."""
    import pandas as pd
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    from broker import Broker
    os.makedirs(cache, exist_ok=True)
    data = Broker().data
    t0 = datetime.fromisoformat(start) - timedelta(days=S33_WARMUP_DAYS)
    t1 = datetime.fromisoformat(end) + timedelta(days=1)
    done = 0
    for sym in symbols:
        path = os.path.join(cache, f"m5_{sym}.csv")
        if os.path.exists(path):
            continue
        req = StockBarsRequest(symbol_or_symbols=sym,
                               timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                               start=t0, end=t1, feed=DataFeed.IEX)
        try:
            df = data.get_stock_bars(req).df
        except Exception as e:
            print(f"{sym}: fetch failed ({e})")
            continue
        if df is None or df.empty:
            continue
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(sym, level="symbol")
        df[["open", "high", "low", "close", "volume"]].to_csv(path)
        done += 1
    return done


def load_m5(sym, cache=S33_CACHE):
    import pandas as pd
    path = os.path.join(cache, f"m5_{sym}.csv")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, index_col=0)
    df.index = pd.to_datetime(df.index, utc=True).tz_convert("America/New_York")
    t = df.index.time
    return df[(t >= RTH[0]) & (t < RTH[1])]


def add_indicators(df):
    import pandas_ta as ta
    df = df.copy()
    df["ema_fast"] = ta.ema(df["close"], length=9)
    df["ema_slow"] = ta.ema(df["close"], length=21)
    adx = ta.adx(df["high"], df["low"], df["close"], length=14)
    df["adx"] = adx["ADX_14"] if adx is not None else float("nan")
    df["rsi"] = ta.rsi(df["close"], length=14)
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["vol_ema"] = ta.ema(df["volume"], length=20)
    return df


def daily_from_m5(df):
    d = df.groupby(df.index.date).agg(open=("open", "first"), high=("high", "max"),
                                      low=("low", "min"), close=("close", "last"))
    return d


def prior_day_map(series_by_day):
    """{day: value as of the PREVIOUS completed session}."""
    out, prev = {}, None
    for day, v in series_by_day.items():
        out[day] = prev
        prev = None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)
    return out


def spy_regime_maps(spy_m5):
    import pandas_ta as ta
    d = daily_from_m5(spy_m5)
    ema = d["close"].ewm(span=20, adjust=False).mean()
    trending = {day: bool(c > e) for day, c, e in zip(d.index, d["close"], ema)}
    adx = ta.adx(d["high"], d["low"], d["close"], length=14)
    adx_s = adx["ADX_14"] if adx is not None else d["close"] * float("nan")
    return prior_day_map(trending), prior_day_map(dict(zip(d.index, adx_s)))


def in_entry_window(ts):
    t = ts.time()
    return any(a <= t < b for a, b in ENTRY_WINDOWS)


def candidate_signals(df, sym, st):
    """Golden crosses that pass every live filter EXCEPT the ADX threshold;
    each carries its ADX so a threshold can be applied afterwards."""
    out = []
    ef, es = df["ema_fast"].values, df["ema_slow"].values
    for j in range(22, len(df) - 2):
        if not (ef[j] > es[j] and ef[j - 1] <= es[j - 1]):
            continue
        row = df.iloc[j]
        if any(math.isnan(float(row[k])) for k in ("adx", "atr")):
            continue
        if st["rsi"] is not None and not (row["rsi"] >= st["rsi"]):
            continue
        k = j + 1
        if st["confirm"] and not (ef[k] > es[k]):
            continue
        if st["volume"] and not (row["volume"] >= row["vol_ema"]):
            continue
        ts = df.index[k] + timedelta(minutes=5)        # entry bar's close
        if not in_entry_window(df.index[k]):
            continue
        out.append({"symbol": sym, "i": k, "ts": ts, "adx": float(row["adx"]),
                    "atr": float(row["atr"]), "entry": float(df["close"].iloc[k])})
    return out


def simulate_intraday(df, i, entry, stop, target, trail_atr):
    """(exit_price, reason, exit_i) for an entry at bar i's close, or None
    if still open at the end of data. The current intraday rule (above)."""
    risk = entry - stop
    live, reached = stop, False
    o, h, l, c = (df[k].values for k in ("open", "high", "low", "close"))
    atr = df["atr"].values
    for n in range(i + 1, len(df)):
        if o[n] <= live:
            return o[n], "stop (gap)", n
        if o[n] >= target:
            return o[n], "target (gap)", n
        if l[n] <= live:
            return live, "stop", n
        if h[n] >= target:
            return target, "target", n
        if not reached and (c[n] - entry) / risk >= 1.0:
            reached = True
        if reached and not math.isnan(atr[n]):
            cand = max(c[n] - trail_atr * atr[n], entry)
            if live < cand < c[n]:
                live = cand
    return None


def s33_replay(frames, spy_m5, st=None, start=S33_START, end=S33_END):
    """{rule label: [trade]} - each trade {symbol, ts, adx, r, why}."""
    import pandas as pd
    st = st or s33_settings()
    spy_trend, spy_adx = spy_regime_maps(spy_m5)
    lo, hi = pd.Timestamp(start).date(), pd.Timestamp(end).date()
    prepared = {}
    for sym, raw in frames.items():
        df = add_indicators(raw)
        datr = prior_day_map(dict(zip(*_daily_atr(raw))))
        sigs = [s for s in candidate_signals(df, sym, st)
                if lo <= s["ts"].date() <= hi and spy_trend.get(s["ts"].date())]
        prepared[sym] = (df, datr, sigs)
    out = {}
    for label, rule in threshold_rules(st["adx"]):
        trades = []
        for sym, (df, datr, sigs) in prepared.items():
            busy_until = -1
            for s in sigs:
                if s["i"] <= busy_until:
                    continue
                if s["adx"] < rule(spy_adx.get(s["ts"].date())):
                    continue
                entry = s["entry"]
                stop = entry - st["atr_mult"] * s["atr"]
                target = entry + st["rr"] * (entry - stop)
                d_atr = datr.get(s["ts"].date())
                if d_atr:
                    target = min(target, entry + st["cap_atr"] * d_atr)
                res = simulate_intraday(df, s["i"], entry, stop, target, st["trail_atr"])
                if res is None:
                    busy_until = len(df)
                    continue
                px, why, n = res
                busy_until = n
                trades.append({"symbol": sym, "ts": s["ts"], "adx": s["adx"],
                               "blocked_live": s["adx"] < st["adx"],
                               "r": (px - entry) / (entry - stop), "why": why})
        trades.sort(key=lambda t: t["ts"])
        out[label] = trades
    return out


def _daily_atr(raw):
    import pandas_ta as ta
    d = daily_from_m5(raw)
    a = ta.atr(d["high"], d["low"], d["close"], length=14)
    return list(d.index), list(a.values) if a is not None else [None] * len(d)


def s33_rows(results, weeks):
    rows = []
    for label, trades in results.items():
        for subset, ts in (("all admitted", trades),
                           ("adx_low only", [t for t in trades if t["blocked_live"]])):
            if label.startswith("current") and subset == "adx_low only":
                continue
            stt = sc.aggregate(ts)
            total = round(sum(t["r"] for t in ts), 2)
            rows.append({"rule": label, "subset": subset, "trades": stt["trades"],
                         "per_week": round(stt["trades"] / weeks, 1) if weeks else None,
                         "win": stt["win_rate"], "exp": stt["expectancy_r"],
                         "total": total, "maxdd": stt["max_drawdown_r"]})
    return rows


def s33_recommend(rows):
    """Only a threshold whose admitted population has expectancy >= +0.15R
    AND more trades than the current one. Best total R among those."""
    cur = next(r for r in rows if r["rule"].startswith("current"))
    ok = [r for r in rows if r["subset"] == "all admitted"
          and not r["rule"].startswith("current")
          and r["exp"] is not None and r["exp"] >= EXPECTANCY_BAR
          and r["trades"] > cur["trades"]]
    if not ok:
        return None, cur
    return max(ok, key=lambda r: r["total"]), cur


def s33_render(rows, n_symbols, weeks, missing):
    best, cur = s33_recommend(rows)
    lines = [f"**S33 (CEO 2026-10-19), study only.** trend_continuation, the only "
             f"setup with an ADX gate, replayed on 5-minute IEX bars "
             f"{S33_START}..{S33_END} ({weeks:.0f} weeks, {n_symbols} universe "
             f"symbols) through the live filter chain and the CURRENT intraday "
             f"exit rule, at four ADX thresholds. \"adx_low only\" = the signals "
             f"the live threshold blocks, admitted by the lower one.", ""]
    lines += sc.table(["Threshold", "Population", "Trades", "Trades/week", "Win %",
                       "Expectancy (R)", "Total R", "MaxDD (R)"],
                      [[r["rule"], r["subset"], r["trades"], r["per_week"],
                        f"{r['win']}%" if r["win"] is not None else "-",
                        f"{r['exp']:+.3f}" if r["exp"] is not None else "-",
                        f"{r['total']:+.2f}", r["maxdd"] if r["maxdd"] is not None else "-"]
                       for r in rows])
    lines.append("")
    if best:
        lines.append(f"**Recommendation:** {best['rule']} - expectancy "
                     f"{best['exp']:+.3f}R (bar +{EXPECTANCY_BAR}R) on {best['trades']} "
                     f"trades vs {cur['trades']} at the current threshold. A "
                     f"recommendation, not a change: the live threshold stays "
                     f"until the CEO rules.")
    else:
        lines.append(f"**No threshold recommended.** None of the lower thresholds "
                     f"reaches expectancy >= +{EXPECTANCY_BAR}R while raising the "
                     f"trade count; the current threshold stands.")
    lines += ["", "**Method.** Filters in live order (cross on the last completed "
              "bar, ADX, RSI >= 55, confirmation, volume >= 20-bar EMA), SPY above "
              "its 20-day EMA, entries 09:30-11:30 / 14:00-15:00 ET at the close of "
              "the bar after the cross; stop 2.0 x ATR14(5m), target 3R capped at "
              "fill + 3 x daily ATR14 (S28), initial stop until +1R then a 2.5 x "
              "ATR14(5m) trail floored at entry; no end-of-day exit. Regime rule: "
              "SPY daily ADX14 >= 25 -> 20, 20-25 -> 25, < 20 -> 30. One position "
              "per symbol; the max-positions cap is not modelled. Reproduce with "
              "`python study_adx_band.py --s33 --fetch` (fetch after the close).",
              "", f"**Limits.** Today's universe (survivorship); IEX volume, as live; "
              f"the gatekeeper is not modelled.{' No bars for: ' + ', '.join(missing) + '.' if missing else ''}",
              "", sc.UPPER_BOUND]
    return lines


def study_universe(path):
    """The universe's symbols from universe_today.json, whatever its date
    (the live loader returns [] for a stale file; a study must not)."""
    try:
        with open(path, encoding="utf-8") as f:
            return [c["symbol"] for c in json.load(f).get("candidates", [])]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def s33_main(args):
    symbols = [s for s in study_universe(args.universe_file) if s != "SPY"]
    if not symbols:
        print(f"No universe symbols in {args.universe_file} - refusing to "
              f"report an empty study.")
        return 2
    if args.fetch:
        print(f"fetched {fetch_m5_year(symbols + ['SPY'])} symbols")
    spy = load_m5("SPY")
    if spy is None:
        print("No SPY bars - run with --fetch after the close.")
        return 2
    frames, missing = {}, []
    for s in symbols:
        df = load_m5(s)
        if df is None or df.empty:
            missing.append(s)
        else:
            frames[s] = df
    results = s33_replay(frames, spy)
    import pandas as pd
    weeks = (pd.Timestamp(S33_END) - pd.Timestamp(S33_START)).days / 7
    lines = s33_render(s33_rows(results, weeks), len(frames), weeks, missing)
    print("\n".join(lines))
    if args.append:
        n = sc.append_agenda("Study: S33 ADX threshold band - trend_continuation, "
                             "5-minute replay 2025-10-14..2026-10-19", lines)
        print(f"\nAppended to BOARDROOM_AGENDA.md as item {n}.")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--append", action="store_true",
                   help="append the table to BOARDROOM_AGENDA.md")
    p.add_argument("--years", type=int, default=sc.YEARS)
    p.add_argument("--s33", action="store_true",
                   help="S33: 5-minute replay of adx_low-blocked signals")
    p.add_argument("--universe-file", default="universe_today.json",
                   help="S33: universe file (default: the live one in cwd)")
    p.add_argument("--fetch", action="store_true",
                   help="S33: fetch missing 5-minute bars (after the close)")
    args = p.parse_args()
    if args.s33:
        return s33_main(args)

    buckets, total, unbanded = run(args.years)
    lines = render(buckets, total, unbanded, args.years)
    print("\n".join(lines))
    if args.append:
        n = sc.append_agenda(
            "Study: trend_continuation expectancy by ADX band (W3a)", lines)
        print(f"\nAppended to BOARDROOM_AGENDA.md as item {n}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
