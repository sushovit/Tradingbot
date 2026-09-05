"""
study_adx_band.py — W3(a). trend_continuation expectancy by ADX band.

    python study_adx_band.py [--append]

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
import sys

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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--append", action="store_true",
                   help="append the table to BOARDROOM_AGENDA.md")
    p.add_argument("--years", type=int, default=sc.YEARS)
    args = p.parse_args()

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
