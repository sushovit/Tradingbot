"""
volume_calibration.py — volume-threshold sweep for momentum + reclaim.

    python volume_calibration.py [--years 3]

Trigger (2026-08-10): are the live volume multipliers (momentum 1.5x,
reclaim 1.2x) set against a higher-volume baseline than summer bars can
clear? Sweeps {1.0, 1.2, 1.3, 1.5} per setup and reports:

  - expectancy, win rate, PF, trade count per threshold (overall)
  - the same split by CALENDAR MONTH (Jul/Aug vs the rest)
  - the expectancy-optimal threshold per setup
  - the direct seasonality evidence: the DISTRIBUTION of volume ratios
    (bar volume / trailing 20-bar average) by calendar month. If summer
    bars genuinely cannot clear a multiplier, the ratio distribution
    itself must be compressed in those months — not merely the trade
    count, which also falls when there is simply less to trade.

Mechanics are backtest.py's exactly (next-bar-open entry, bracket exits,
3R target, whole shares, 25% cap, 1% risk on $2,000). The REAL detectors
are used with their module constant overridden, so the study can never
drift from live logic.

RESEARCH ONLY. Rule changes require mini-boardroom ratification.
"""

import argparse
import json
import sys
from collections import Counter, defaultdict

import pandas as pd

import backtest
from strategies import mean_reversion_reclaim as reclaim_mod
from strategies import momentum_continuation as momentum_mod

THRESHOLDS = [1.0, 1.2, 1.3, 1.5]
TARGET_R = 3.0
REPORT_FILE = "volume_calibration.md"
SETUPS = {
    "momentum_continuation": (momentum_mod, 1.5),   # (module, live value)
    "mean_reversion_reclaim": (reclaim_mod, 1.2),
}
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def volume_ratio_by_month(bars: dict) -> dict:
    """month number -> list of (bar volume / trailing 20-bar mean) ratios.
    This is the seasonality evidence that does not depend on any rule."""
    per_month = defaultdict(list)
    for df in bars.values():
        if df is None or len(df) < 25:
            continue
        # shift(1): compare each bar against the PRIOR 20 bars, excluding
        # itself — the same window the detectors use. Including the bar in
        # its own average deflates every spike (a 3x bar reads as 2.7x).
        avg20 = df["volume"].rolling(20).mean().shift(1)
        ratio = (df["volume"] / avg20).replace([float("inf")], pd.NA).dropna()
        for ts, r in ratio.items():
            if pd.notna(r) and r > 0:
                per_month[pd.Timestamp(ts).month].append(float(r))
    return per_month


def sweep_setup(setup: str, bars: dict, regime, thresholds=THRESHOLDS) -> dict:
    """Run the REAL detector at each volume threshold. Returns
    {threshold: trades}."""
    module, live_value = SETUPS[setup]
    original = module.VOLUME_MULT
    out = {}
    try:
        for thr in thresholds:
            module.VOLUME_MULT = thr
            trades = []
            for sym, df in bars.items():
                sigs, _ = backtest.collect_signals(
                    sym, df, setup, regime,
                    backtest.BASE_RISK_PROFILE, backtest.BASE_CONFIG)
                trades.extend(backtest.trades_from_signals(
                    sym, df, setup, sigs, TARGET_R))
            out[thr] = trades
    finally:
        module.VOLUME_MULT = original      # never leave the module mutated
    return out


def by_month(trades: list) -> dict:
    """calendar month number -> trades in that month (any year)."""
    grouped = defaultdict(list)
    for t in trades:
        try:
            grouped[int(t["signal_date"][5:7])].append(t)
        except (ValueError, KeyError, IndexError):
            continue
    return grouped


def optimal_threshold(sweep: dict):
    """Threshold with the best expectancy, ignoring samples too small to
    mean anything (< 30 trades)."""
    best, best_e = None, None
    for thr, trades in sweep.items():
        stats = backtest.aggregate(trades)
        if stats["trades"] < 30 or stats["expectancy_r"] is None:
            continue
        if best_e is None or stats["expectancy_r"] > best_e:
            best, best_e = thr, stats["expectancy_r"]
    return best, best_e


def run(symbols, years=3):
    import clockline
    bars = backtest.load_universe_bars(list(symbols) + ["SPY"], years=years)
    spy = bars.pop("SPY", None)
    regime = backtest.spy_regime_series(spy) if spy is not None else None

    lines = ["# Volume-threshold calibration", clockline.two_zone_line(), "",
             f"Universe: {len(bars)} tickers | {years}y daily bars | "
             f"{TARGET_R:.0f}R target, next-bar-open entry, 1% risk on $2,000",
             "", "RESEARCH ONLY — rule changes require mini-boardroom "
             "ratification.", ""]

    # ---------- seasonality evidence, independent of any rule ----------
    ratios = volume_ratio_by_month(bars)
    lines.append("## Volume-ratio distribution by calendar month")
    lines.append("_bar volume / trailing 20-bar average. The share clearing "
                 "each multiplier is the direct test of the seasonal-thinness "
                 "hypothesis._")
    lines.append("| Month | Bars | Median ratio | % >= 1.2x | % >= 1.3x | % >= 1.5x |")
    lines.append("|---|---|---|---|---|---|")
    for m in range(1, 13):
        vals = ratios.get(m, [])
        if not vals:
            continue
        s = pd.Series(vals)
        lines.append(
            f"| {MONTH_NAMES[m - 1]} | {len(vals)} | {s.median():.2f} "
            f"| {100 * (s >= 1.2).mean():.1f}% | {100 * (s >= 1.3).mean():.1f}% "
            f"| {100 * (s >= 1.5).mean():.1f}% |")

    summer = [r for m in (7, 8) for r in ratios.get(m, [])]
    rest = [r for m in range(1, 13) if m not in (7, 8) for r in ratios.get(m, [])]
    if summer and rest:
        s_sum, s_rest = pd.Series(summer), pd.Series(rest)
        lines.append("")
        lines.append(f"**Jul-Aug vs rest of year**: median ratio "
                     f"{s_sum.median():.3f} vs {s_rest.median():.3f}; "
                     f"share clearing 1.5x {100 * (s_sum >= 1.5).mean():.1f}% "
                     f"vs {100 * (s_rest >= 1.5).mean():.1f}%.")
        verdict = ("SUPPORTED — summer bars clear the multipliers materially "
                   "less often"
                   if (s_sum >= 1.5).mean() < (s_rest >= 1.5).mean() * 0.85
                   else "NOT SUPPORTED — summer bars clear the multipliers at "
                        "a similar rate; the ratio is self-normalising against "
                        "its own trailing average")
        lines.append(f"**Seasonal-thinness hypothesis: {verdict}.**")

    # ---------- threshold sweep per setup ----------
    results = {}
    for setup in SETUPS:
        sweep = results[setup] = sweep_setup(setup, bars, regime)
        live = SETUPS[setup][1]
        lines.append(f"\n## {setup} — threshold sweep (live: {live}x)")
        lines.append("| Volume >= | Trades | Win% | Expectancy (R) | PF | MaxDD (R) |")
        lines.append("|---|---|---|---|---|---|")
        for thr in THRESHOLDS:
            st = backtest.aggregate(sweep[thr])
            mark = "  <-- LIVE" if abs(thr - live) < 1e-9 else ""
            lines.append(f"| {thr}x | {st['trades']} | {st['win_rate']} "
                         f"| {st['expectancy_r']} | {st['profit_factor']} "
                         f"| {st['max_drawdown_r']} |{mark}")
        best, best_e = optimal_threshold(sweep)
        lines.append(f"\n**Expectancy-optimal threshold: "
                     f"{best if best else 'n/a'}x** "
                     f"({best_e:+.3f}R)" if best else
                     "\n**Expectancy-optimal threshold: n/a** (no threshold "
                     "reached a 30-trade minimum)")

        lines.append(f"\n### {setup} by calendar month")
        lines.append("| Month | " + " | ".join(
            f"{t}x trades / exp" for t in THRESHOLDS) + " |")
        lines.append("|---" * (len(THRESHOLDS) + 1) + "|")
        monthly = {thr: by_month(sweep[thr]) for thr in THRESHOLDS}
        for m in range(1, 13):
            cells = []
            for thr in THRESHOLDS:
                st = backtest.aggregate(monthly[thr].get(m, []))
                cells.append(f"{st['trades']} / "
                             f"{st['expectancy_r'] if st['expectancy_r'] is not None else '—'}")
            if any(not c.startswith("0 /") for c in cells):
                lines.append(f"| {MONTH_NAMES[m - 1]} | " + " | ".join(cells) + " |")

        # Jul-Aug rollup at the live threshold vs the rest
        live_trades = sweep[live]
        ja = [t for t in live_trades if t["signal_date"][5:7] in ("07", "08")]
        other = [t for t in live_trades if t["signal_date"][5:7] not in ("07", "08")]
        st_ja, st_o = backtest.aggregate(ja), backtest.aggregate(other)
        lines.append(f"\n**At the live {live}x**: Jul-Aug {st_ja['trades']} trades "
                     f"({st_ja['expectancy_r']}R), rest of year {st_o['trades']} "
                     f"({st_o['expectancy_r']}R).")

    report = "\n".join(lines) + "\n"
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(report)
    return results, report


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--years", type=int, default=3)
    args = p.parse_args()
    try:
        with open("bot_config.json") as f:
            watchlist = json.load(f).get("universe", {}).get("core_watchlist", [])
    except (FileNotFoundError, json.JSONDecodeError):
        watchlist = []
    if not watchlist:
        print("No core_watchlist in bot_config.json.")
        return 1
    _, report = run(watchlist, years=args.years)
    print(report)
    print(f"Written: {REPORT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
