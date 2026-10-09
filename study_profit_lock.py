"""
study_profit_lock.py — S15. Stepped profit lock for the daily setups, on the
SAME entries as the static-stop control.

    python study_profit_lock.py [--append]

Variants (trigger on a bar's CLOSE, applied from the next bar, never
lowered):
  a  static                     - structural stop never moves (10.6)
  b  BE at +1R                  - close >= +1R   -> stop to entry
  c  BE at +1.5R                - close >= +1.5R -> stop to entry
  d  +2R -> +1R                 - close >= +2R   -> stop to +1R
  e  +1.5R -> +0.5R, +2.5R -> +1.5R
  f  BE at +1R, then +2R -> +1R

Fill rules are study_common.simulate_exit's: a bar opening through a level
fills at the open; a bar spanning both levels hits the STOP first. Target
3R, as the live brackets. "Gave back" = the trade's bar HIGH reached +1.5R
at some point and the trade still closed at a loss.
"""

import argparse
import sys

import study_common as sc

TARGET_R = 3.0
SETUPS = ("momentum_continuation", "mean_reversion_reclaim")
VARIANTS = (
    ("a static", ()),
    ("b BE at +1R", ((1.0, 0.0),)),
    ("c BE at +1.5R", ((1.5, 0.0),)),
    ("d +2R -> +1R", ((2.0, 1.0),)),
    ("e +1.5R->+0.5R, +2.5R->+1.5R", ((1.5, 0.5), (2.5, 1.5))),
    ("f BE at +1R, +2R->+1R", ((1.0, 0.0), (2.0, 1.0))),
)


def run(years: int = sc.YEARS, setups=SETUPS):
    import backtest
    bars, regime = sc.load_bars(years=years)
    profile, config = sc.study_profile(), sc.study_config()
    results = {}
    for setup in setups:
        signals = {}
        for symbol, df in bars.items():
            sigs, _ = backtest.collect_signals(symbol, df, setup, regime,
                                               profile, config)
            if sigs:
                signals[symbol] = sigs
        results[setup] = {
            label: [t for symbol, sigs in signals.items()
                    for t in sc.lock_trades_from(bars[symbol], symbol, setup,
                                                 sigs, steps,
                                                 target_r=TARGET_R)]
            for label, steps in VARIANTS}
    return results


def gave_back_pct(trades):
    if not trades:
        return "—"
    n = sum(1 for t in trades if t["mfe_r"] >= 1.5 and t["r"] < 0)
    return f"{100 * n / len(trades):.1f}%"


def render(results, years):
    lines = [
        f"**Question.** Which profit lock should replace the static stop "
        f"(10.6) for daily setups? Same entries per setup, same fill rules, "
        f"{years}y daily, {TARGET_R:g}R target.",
        "",
    ]
    rows = []
    for setup, per_variant in results.items():
        for label, _ in VARIANTS:
            trades = per_variant[label]
            stats = sc.aggregate(trades)
            rows.append([setup, label] + sc.fmt(stats)
                        + [stats["max_drawdown_r"] if stats["trades"] else "—",
                           gave_back_pct(trades)])
    lines += sc.table(["Setup", "Variant", "Trades", "Win %", "Avg R",
                       "Expectancy", "PF", "MaxDD (R)",
                       "Hit +1.5R, closed at a loss"], rows)
    lines += ["", "**Exit mix:**", ""]
    mix = []
    for setup, per_variant in results.items():
        for label, _ in VARIANTS:
            trades = per_variant[label]
            count = lambda k: sum(1 for t in trades if t["exit_reason"] == k)
            locked = sum(1 for t in trades if t["exit_reason"] == "stop"
                         and t["r"] >= 0)
            mix.append([setup, label, count("target") + count("gap_target"),
                        count("stop") + count("gap_stop") - locked, locked])
    lines += sc.table(["Setup", "Variant", "target", "stop at a loss",
                       "stop at >= 0R (locked)"], mix)
    lines += [
        "",
        "**Method.** `study_common.simulate_lock`: the step is decided on a "
        "bar's CLOSE and applies from the NEXT bar; a stop never moves down; "
        "a bar spanning stop and target hits the stop first; gaps fill at "
        "the open. One position per symbol at a time. The live rule will "
        "trigger on the last completed 5-minute close, which reacts sooner "
        "than a daily close, so these rows are a conservative read of how "
        "often each lock engages. Reproduce with "
        "`python study_profit_lock.py`.",
        "",
        sc.UPPER_BOUND,
    ]
    return lines


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--append", action="store_true")
    p.add_argument("--years", type=int, default=sc.YEARS)
    args = p.parse_args()
    results = run(args.years)
    print("\n".join(render(results, args.years)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
