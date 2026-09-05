"""
study_day_one.py — W3(e). Day-one participation: does the desk get on board
after a big up-day, and is that entry worth having?

    python study_day_one.py [--append]

Question the boardroom asked: for universe-class names, how often was a
>= 7% up-day at or near the 20-day high followed by a momentum_continuation
signal the NEXT session, and what was that signal's expectancy? This is the
evidence for or against commissioning a gap-and-go lane.

Definitions, stated because they decide the answer:
  - "up-day": close-over-close change >= 7% on a completed daily bar
  - "at/near the 20-day high": the day's close within NEAR_HIGH_PCT of the
    highest high of the prior 20 bars (a 7% pop out of a downtrend base is
    not the setup being asked about)
  - "next session": the very next completed bar produces a
    momentum_continuation signal whose ENTRY bar is the session after it

Participation is measured against the detector as it actually runs, so a
"miss" means the live desk would not have taken it.
"""

import argparse
import sys

import study_common as sc

UP_DAY_PCT = 7.0
NEAR_HIGH_PCT = 2.0
TARGET_R = 3.0


def up_days(df, lookback: int = 20):
    """Positions of completed bars that were >= UP_DAY_PCT up AND closed
    at/near the prior 20-bar high."""
    hits = []
    closes = df["close"].values
    highs = df["high"].values
    for i in range(lookback + 1, len(df) - 1):     # -1: need a next session
        prev = closes[i - 1]
        if prev <= 0:
            continue
        change = (closes[i] / prev - 1) * 100
        if change < UP_DAY_PCT:
            continue
        prior_high = highs[i - lookback:i].max()
        if prior_high <= 0:
            continue
        if closes[i] < prior_high * (1 - NEAR_HIGH_PCT / 100):
            continue                                # popped, but off the base
        hits.append((i, change))
    return hits


def run(years: int = sc.YEARS):
    bars, regime = sc.load_bars(years=years)
    profile, config = sc.study_profile(), sc.study_config()
    import backtest

    total_events = 0
    followed = []          # signals whose entry is the session after the pop
    per_symbol_events = {}

    for symbol, df in bars.items():
        events = up_days(df)
        if not events:
            continue
        per_symbol_events[symbol] = len(events)
        total_events += len(events)

        sigs, _ = backtest.collect_signals(symbol, df, "momentum_continuation",
                                           regime, profile, config)
        # a signal's ENTRY bar is entry_i; the pop is the bar before the
        # signal bar, i.e. entry_i - 2, or the signal bar itself (entry_i - 1)
        entry_index = {s["entry_i"]: s for s in sigs}
        for pos, change in events:
            sig = entry_index.get(pos + 1)          # pop bar IS the signal bar
            if sig is not None:
                followed.append((symbol, sig, change))

    trades = []
    by_symbol = {}
    for symbol, sig, change in followed:
        by_symbol.setdefault(symbol, []).append((sig, change))
    for symbol, pairs in by_symbol.items():
        pairs.sort(key=lambda p: p[0]["entry_i"])
        made = sc.trades_from(bars[symbol], symbol, "momentum_continuation",
                              [p[0] for p in pairs], target_r=TARGET_R,
                              mode="static")
        trades.extend(made)

    return {"events": total_events, "followed": len(followed),
            "trades": trades, "symbols": len(per_symbol_events)}


def render(result, years):
    events, followed = result["events"], result["followed"]
    rate = (100.0 * followed / events) if events else 0.0
    stats = sc.aggregate(result["trades"])

    lines = [
        f"**Question.** After a >= {UP_DAY_PCT:.0f}% up-day at or near the "
        f"20-day high, does momentum_continuation get the desk on board the "
        f"next session, and is that entry worth having? {years}y daily over "
        f"{result['symbols']} names with such days.",
        "",
    ]
    lines += sc.table(
        ["Qualifying up-days", "Followed by a signal", "Participation rate"],
        [[events, followed, f"{rate:.1f}%"]])
    lines += ["", "**Expectancy of the signals that did fire:**", ""]
    lines += sc.table(["Trades", "Win %", "Avg R", "Expectancy", "PF",
                       "MaxDD (R)"],
                      [sc.fmt(stats)[0:5]
                       + [stats["max_drawdown_r"] if stats["trades"] else "—"]])
    lines += [
        "",
        f"**Reading.** Participation is {rate:.1f}%: that is the share of big "
        f"up-days the existing detector already converts into an entry the "
        f"next session. The gap between that and 100% is the space a "
        f"dedicated gap-and-go lane would occupy — but the missed days are "
        f"missed because momentum_continuation's own filters (20-bar-high "
        f"breakout, volume, +3% change) rejected them, so the residual is not "
        f"free money; it is the set of pops those filters deliberately "
        f"declined. Commissioning a lane is worth it only if a DIFFERENT "
        f"thesis explains that residual, not merely the fact that it exists.",
        "",
        f"**Definitions.** Up-day = close-over-close >= {UP_DAY_PCT:.0f}%; "
        f"near the high = close within {NEAR_HIGH_PCT:.0f}% of the prior "
        f"20-bar high (a pop out of a downtrend base is excluded); next "
        f"session = the pop bar is the signal bar and the fill is the "
        f"following open. {TARGET_R}R target. Reproduce with "
        f"`python study_day_one.py`.",
        "",
        sc.UPPER_BOUND,
    ]
    return lines


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--append", action="store_true")
    p.add_argument("--years", type=int, default=sc.YEARS)
    args = p.parse_args()

    result = run(args.years)
    lines = render(result, args.years)
    print("\n".join(lines))
    if args.append:
        n = sc.append_agenda(
            "Study: day-one participation after a big up-day (W3e)", lines)
        print(f"\nAppended to BOARDROOM_AGENDA.md as item {n}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
