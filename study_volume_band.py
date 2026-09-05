"""
study_volume_band.py — W3(b). Reclaim and momentum expectancy by signal
volume band.

    python study_volume_band.py [--append]

Question: is the 1.3x volume multiplier earning its keep, or would 1.0-1.3x
signals have traded just as well?

METHOD NOTE. The live config runs a 1.3x multiplier for both setups, so no
signal below 1.3x has ever been generated. This study lowers its own
multiplier to 1.0 (study-local config; bot_config.json untouched) to admit
the wider population, then buckets by the ratio observed on the signal bar.
"""

import argparse
import sys

import study_common as sc

STUDY_VOLUME_FLOOR = 1.0
TARGET_R = 3.0
SETUPS = ("mean_reversion_reclaim", "momentum_continuation")
BANDS = (("1.0-1.3x", 1.0, 1.3), (">= 1.3x", 1.3, float("inf")))


def run(years: int = sc.YEARS):
    bars, regime = sc.load_bars(years=years)
    profile = sc.study_profile()
    config = sc.study_config(volume_multipliers={
        name: STUDY_VOLUME_FLOOR for name in SETUPS})

    results = {}
    for setup in SETUPS:
        signals = sc.collect_with_features(
            bars, regime, setup, profile, config,
            feature_fns={"vol_ratio": sc.volume_ratio_at})
        by_symbol = {}
        for sig in signals:
            by_symbol.setdefault(sig["symbol"], []).append(sig)

        trades = []
        for symbol, sigs in by_symbol.items():
            sigs.sort(key=lambda s: s["entry_i"])
            made = sc.trades_from(bars[symbol], symbol, setup, sigs,
                                  target_r=TARGET_R, mode="static")
            for trade, sig in zip(made, sigs[:len(made)]):
                trade["vol_ratio"] = sig.get("vol_ratio")
            trades.extend(made)

        buckets = {name: [] for name, _, _ in BANDS}
        unbanded = 0
        for trade in trades:
            ratio = trade.get("vol_ratio")
            if ratio is None:
                unbanded += 1
                continue
            for name, low, high in BANDS:
                if low <= ratio < high:
                    buckets[name].append(trade)
                    break
            else:
                unbanded += 1
        results[setup] = (buckets, len(trades), unbanded)
    return results


def render(results, years):
    lines = [
        f"**Question.** Is the 1.3x volume multiplier earning its keep, or "
        f"would 1.0-1.3x signals have traded as well? {years}y daily, "
        f"{TARGET_R}R target.",
        "",
    ]
    rows = []
    for setup, (buckets, total, unbanded) in results.items():
        for name in buckets:
            stats = sc.aggregate(buckets[name])
            rows.append([setup, name] + sc.fmt(stats))
    lines += sc.table(["Setup", "Volume band", "Trades", "Win %", "Avg R",
                       "Expectancy", "PF"], rows)
    excluded = sum(u for _, _, u in results.values())
    lines += [
        "",
        f"**Method.** The live config runs a 1.3x multiplier for both setups, "
        f"so sub-1.3x signals were never generated and cannot be read from "
        f"live history. This study lowered its own multiplier to "
        f"{STUDY_VOLUME_FLOOR} to admit them, then bucketed by the ratio on "
        f"each signal bar (bar volume over its trailing 20-bar average). "
        f"`bot_config.json` was not modified. Reproduce with "
        f"`python study_volume_band.py`.",
    ]
    if excluded:
        lines.append(f"{excluded} trade(s) had no computable volume ratio "
                     f"(insufficient history) and are excluded.")
    lines += ["", sc.UPPER_BOUND]
    return lines


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--append", action="store_true")
    p.add_argument("--years", type=int, default=sc.YEARS)
    args = p.parse_args()

    results = run(args.years)
    lines = render(results, args.years)
    print("\n".join(lines))
    if args.append:
        n = sc.append_agenda(
            "Study: reclaim and momentum expectancy by volume band (W3b)",
            lines)
        print(f"\nAppended to BOARDROOM_AGENDA.md as item {n}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
