"""
study_exit_rule.py — W3(c). Exit rule per setup: breakeven floor at +1R
(ratified 2026-09-03) vs pure ATR trail (the previous behaviour), on the
SAME entries.

    python study_exit_rule.py [--append]

WHAT HAD TO BE BUILT. backtest.py could not answer this: simulate_bracket
models a STATIC stop and never trails, so the repository had no simulation
of either exit rule. study_common.simulate_exit adds one, with three modes
sharing identical fill conventions so the comparison isolates the stop:

  static — structural stop, never moves (what backtest_report.md reports)
  atr    — from +1R, trail at close - ATR*mult, monotonic upward
  floor  — as atr, but from +1R stop = max(ATR trail, entry)

The trail is recomputed from a bar's CLOSE and applies from the NEXT bar, so
no trade is exited on information its bar had not yet produced. A bar that
spans both levels is assumed to hit the STOP first, as elsewhere.
"""

import argparse
import sys

import study_common as sc

TARGET_R = 3.0
ATR_MULT = 2.5          # backtest BASE_RISK_PROFILE trailing_stop_value
SETUPS = ("trend_continuation", "momentum_continuation",
          "mean_reversion_reclaim")
MODES = (("static stop", "static"), ("ATR trail", "atr"),
         ("breakeven floor", "floor"))


def run(years: int = sc.YEARS, setups=SETUPS):
    bars, regime = sc.load_bars(years=years)
    profile, config = sc.study_profile(), sc.study_config()

    results = {}
    for setup in setups:
        # Collect ONCE; every mode replays the identical entry list.
        signals = {}
        for symbol, df in bars.items():
            sigs, _ = backtest_collect(symbol, df, setup, regime, profile,
                                       config)
            if sigs:
                signals[symbol] = sigs
        per_mode = {}
        for label, mode in MODES:
            trades = []
            for symbol, sigs in signals.items():
                trades.extend(sc.trades_from(bars[symbol], symbol, setup,
                                             sigs, target_r=TARGET_R,
                                             mode=mode, atr_mult=ATR_MULT))
            per_mode[label] = trades
        results[setup] = per_mode
    return results


def backtest_collect(symbol, df, setup, regime, profile, config):
    import backtest
    return backtest.collect_signals(symbol, df, setup, regime, profile,
                                    config)


def exit_mix(trades):
    kinds = ("target", "stop", "gap_target", "gap_stop")
    counts = {k: sum(1 for t in trades if t["exit_reason"] == k)
              for k in kinds}
    return counts


def render(results, years):
    lines = [
        f"**Question.** Does the breakeven floor ratified 2026-09-03 improve "
        f"expectancy, or does it just cut winners short? Same entries, same "
        f"fill rules, {years}y daily, {TARGET_R}R target, ATR x{ATR_MULT}.",
        "",
    ]
    rows = []
    for setup, per_mode in results.items():
        for label, _ in MODES:
            stats = sc.aggregate(per_mode[label])
            rows.append([setup, label] + sc.fmt(stats)
                        + [stats["max_drawdown_r"] if stats["trades"] else "—"])
    lines += sc.table(["Setup", "Exit rule", "Trades", "Win %", "Avg R",
                       "Expectancy", "PF", "MaxDD (R)"], rows)

    lines += ["", "**Exit mix** (how each rule ends its trades):", ""]
    mix_rows = []
    for setup, per_mode in results.items():
        for label, _ in MODES:
            counts = exit_mix(per_mode[label])
            mix_rows.append([setup, label, counts["target"], counts["stop"],
                             counts["gap_target"], counts["gap_stop"]])
    lines += sc.table(["Setup", "Exit rule", "target", "stop", "gap_target",
                       "gap_stop"], mix_rows)

    lines += [
        "",
        "**Method.** `backtest.py` could not answer this — `simulate_bracket` "
        "models a static stop and never trails, so neither exit rule had ever "
        "been simulated. `study_common.simulate_exit` adds the trail, with "
        "all three modes sharing identical fill conventions so the comparison "
        "isolates the stop and nothing else. The trail is recomputed from a "
        "bar's CLOSE and applies from the NEXT bar, so no trade exits on "
        "information its own bar had not yet produced; a bar spanning both "
        "levels is assumed to hit the stop first. Reproduce with "
        "`python study_exit_rule.py`.",
        "",
        "_The 'static stop' row is the column reported in "
        "`backtest_report.md`, included here as the control._",
        "",
        sc.UPPER_BOUND,
    ]
    return lines


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--append", action="store_true")
    p.add_argument("--years", type=int, default=sc.YEARS)
    p.add_argument("--setups", nargs="*", default=list(SETUPS))
    args = p.parse_args()

    results = run(args.years, tuple(args.setups))
    lines = render(results, args.years)
    print("\n".join(lines))
    if args.append:
        n = sc.append_agenda(
            "Study: exit rule — breakeven floor vs pure ATR trail (W3c)",
            lines)
        print(f"\nAppended to BOARDROOM_AGENDA.md as item {n}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
