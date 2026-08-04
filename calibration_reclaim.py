"""
calibration_reclaim.py — deep-washout reclaim calibration study.

    python calibration_reclaim.py [--years 3] [--threshold 25]

Boardroom question (2026-08-04): for reclaims where the washout exceeded
25% from the 20-day high, is the current EMA9 trigger too slow? Measures,
per variant and per regime:

  - trades, win rate, avg R, expectancy, profit factor, max drawdown
  - BARS FROM BOUNCE-LOW TO SIGNAL COMPLETION (how late we arrive)
  - "NO ROOM LEFT": how often the signal completes within 2% of the
    20-bar high, the failure today exposed

Variants
  current : close > prior-day high AND close > EMA9      (live rule)
  (a)     : EMA9 rule OR two consecutive closes above prior-day highs,
            whichever fires FIRST
  (b)     : close > prior-day high AND close > 5-day EMA (EMA9 replaced)

Mechanics mirror backtest.py exactly: next-bar-OPEN entry, stop at the
signal bar's low, 3R target, whole shares, 25% cap, 1% risk on $2,000,
gap-aware fills. RESEARCH ONLY — no live wiring. Rule changes require
boardroom ratification.
"""

import argparse
import json
import sys

import pandas as pd
import pandas_ta as ta

import backtest
import risk

DEEP_WASHOUT_PCT = 25.0
LOOKBACK = 20
VOLUME_MULT = 1.2
NO_ROOM_PCT = 2.0          # within 2% of the 20-bar high = "no room left"
TARGET_R = 3.0
REPORT_FILE = "calibration_reclaim.md"


def _prep(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema_9"] = ta.ema(out["close"], length=9)
    out["ema_5"] = ta.ema(out["close"], length=5)
    return out


def deep_washout_context(window: pd.DataFrame, threshold: float):
    """Return (drawdown_pct, bounce_low_idx, high_20) if the window shows a
    washout deeper than `threshold` from its 20-bar high, else None."""
    w = window.iloc[-(LOOKBACK + 2):-1]
    if len(w) < 5:
        return None
    high_20 = float(w["high"].max())
    if high_20 <= 0:
        return None
    low_pos = w["low"].values.argmin()
    low_20 = float(w["low"].values[low_pos])
    drawdown = (high_20 - low_20) / high_20 * 100
    if drawdown < threshold:
        return None
    return drawdown, w.index[low_pos], high_20


def variant_triggers(window: pd.DataFrame, variant: str) -> bool:
    """Does the signal bar (last completed) trigger under this variant?"""
    bar = window.iloc[-2]
    prior = window.iloc[-3]
    above_prior_high = float(bar["close"]) > float(prior["high"])

    if variant == "current":
        ema9 = bar.get("ema_9")
        return above_prior_high and pd.notna(ema9) and float(bar["close"]) > float(ema9)

    if variant == "a_ema9_or_two_closes":
        ema9 = bar.get("ema_9")
        ema9_ok = above_prior_high and pd.notna(ema9) \
            and float(bar["close"]) > float(ema9)
        # two CONSECUTIVE closes above the prior day's high
        prior2 = window.iloc[-4]
        two_closes = (above_prior_high
                      and float(prior["close"]) > float(prior2["high"]))
        return ema9_ok or two_closes

    if variant == "b_ema5":
        ema5 = bar.get("ema_5")
        return above_prior_high and pd.notna(ema5) and float(bar["close"]) > float(ema5)

    raise ValueError(variant)


def scan_symbol(symbol: str, df: pd.DataFrame, regime, variant: str,
                threshold: float) -> list:
    """Replay one variant over one symbol. No lookahead: detection uses a
    window ending at the ENTRY bar, whose OPEN is the fill."""
    df = _prep(df)
    trades, busy_until = [], -1
    for i in range(backtest.WARMUP_BARS, len(df)):
        if i <= busy_until:
            continue
        window = df.iloc[max(0, i - 60 + 1):i + 1]
        if len(window) < 6:
            continue
        ctx = deep_washout_context(window, threshold)
        if ctx is None:
            continue
        drawdown, bounce_low_day, high_20 = ctx
        if not variant_triggers(window, variant):
            continue

        bar = window.iloc[-2]
        w = window.iloc[-(LOOKBACK + 2):-1]
        avg_vol = float(w["volume"].mean())
        if avg_vol <= 0 or float(bar["volume"]) <= avg_vol * VOLUME_MULT:
            continue

        entry = float(df["open"].iloc[i])
        stop = float(bar["low"])
        if stop >= entry:
            continue
        target = max(high_20, entry + (entry - stop) * TARGET_R)
        qty = risk.position_size(backtest.BASE_EQUITY, backtest.RISK_PCT,
                                 entry, stop,
                                 position_cap_pct=backtest.POSITION_CAP)
        if qty < 1:
            continue

        exit_price, exit_i, reason = backtest.simulate_bracket(
            df, i, entry, stop, target)
        if exit_price is None:
            break
        busy_until = exit_i

        # How LATE did we arrive? bars from the bounce low to the signal bar.
        try:
            bounce_pos = df.index.get_loc(bounce_low_day)
            bars_from_low = (i - 1) - bounce_pos
        except KeyError:
            bars_from_low = None
        # "No room left": signal completes within 2% of the 20-bar high.
        room_pct = (high_20 - float(bar["close"])) / high_20 * 100 \
            if high_20 else None
        sig_day = df.index[i - 1]
        in_regime = bool(regime.reindex([sig_day]).fillna(False).iloc[0]) \
            if regime is not None else True

        trades.append({
            "symbol": symbol, "variant": variant,
            "signal_date": str(sig_day)[:10],
            "entry": round(entry, 2), "stop": round(stop, 2),
            "exit": round(float(exit_price), 2),
            "r": round((exit_price - entry) / (entry - stop), 3),
            "pnl_usd": round((exit_price - entry) * qty, 2),
            "exit_reason": reason,
            "regime": "trending" if in_regime else "chop",
            "drawdown_pct": round(drawdown, 1),
            "bars_from_low": bars_from_low,
            "room_to_high_pct": round(room_pct, 2) if room_pct is not None else None,
            "no_room": (room_pct is not None and room_pct < NO_ROOM_PCT),
        })
    return trades


def summarize(trades: list) -> dict:
    stats = backtest.aggregate(trades)
    bars = [t["bars_from_low"] for t in trades if t["bars_from_low"] is not None]
    no_room = [t for t in trades if t["no_room"]]
    stats["median_bars_from_low"] = (
        round(float(pd.Series(bars).median()), 1) if bars else None)
    stats["mean_bars_from_low"] = (
        round(sum(bars) / len(bars), 1) if bars else None)
    stats["no_room_pct"] = (round(100 * len(no_room) / len(trades), 1)
                            if trades else None)
    stats["no_room_expectancy"] = backtest.aggregate(no_room)["expectancy_r"]
    has_room = [t for t in trades if not t["no_room"]]
    stats["has_room_expectancy"] = backtest.aggregate(has_room)["expectancy_r"]
    return stats


VARIANTS = [
    ("current", "close > prior-day high AND close > EMA9 (live rule)"),
    ("a_ema9_or_two_closes", "(a) EMA9 rule OR two consecutive closes above "
                             "prior-day highs, whichever first"),
    ("b_ema5", "(b) close > prior-day high AND close > 5-day EMA"),
]


def run(symbols, years=3, threshold=DEEP_WASHOUT_PCT):
    import clockline
    bars = backtest.load_universe_bars(list(symbols) + ["SPY"], years=years)
    spy = bars.pop("SPY", None)
    regime = backtest.spy_regime_series(spy) if spy is not None else None

    results = {}
    for variant, _ in VARIANTS:
        trades = []
        for sym, df in bars.items():
            trades.extend(scan_symbol(sym, df, regime, variant, threshold))
        results[variant] = trades

    lines = [f"# Reclaim calibration — washouts deeper than {threshold:.0f}%",
             clockline.two_zone_line(), "",
             f"Universe: {len(bars)} tickers | {years}y daily bars | "
             f"{TARGET_R:.0f}R target, stop at signal-bar low, next-bar-open "
             f"entry, 1% risk on $2,000", "",
             "RESEARCH ONLY — no live wiring. Rule changes require boardroom "
             "ratification.", ""]

    lines.append("## Variants")
    for v, desc in VARIANTS:
        lines.append(f"- **{v}**: {desc}")

    lines.append("\n## Overall (all regimes)")
    lines.append("| Variant | Trades | Win% | Expectancy (R) | PF | MaxDD (R) "
                 "| Median bars from bounce low | No-room % |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for v, _ in VARIANTS:
        s = summarize(results[v])
        lines.append(f"| {v} | {s['trades']} | {s['win_rate']} "
                     f"| {s['expectancy_r']} | {s['profit_factor']} "
                     f"| {s['max_drawdown_r']} | {s['median_bars_from_low']} "
                     f"| {s['no_room_pct']}% |")

    lines.append("\n## By regime")
    lines.append("| Variant | Regime | Trades | Win% | Expectancy (R) | PF "
                 "| Median bars from low | No-room % |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for v, _ in VARIANTS:
        for reg in ("trending", "chop"):
            s = summarize([t for t in results[v] if t["regime"] == reg])
            lines.append(f"| {v} | {reg} | {s['trades']} | {s['win_rate']} "
                         f"| {s['expectancy_r']} | {s['profit_factor']} "
                         f"| {s['median_bars_from_low']} | {s['no_room_pct']}% |")

    lines.append("\n## The 'no room left' failure (signal completes within "
                 f"{NO_ROOM_PCT:.0f}% of the 20-bar high)")
    lines.append("| Variant | No-room trades | No-room % | Expectancy WITH room "
                 "| Expectancy NO room |")
    lines.append("|---|---|---|---|---|")
    for v, _ in VARIANTS:
        s = summarize(results[v])
        n = sum(1 for t in results[v] if t["no_room"])
        lines.append(f"| {v} | {n} | {s['no_room_pct']}% "
                     f"| {s['has_room_expectancy']} | {s['no_room_expectancy']} |")

    lines.append("\n## Arrival lateness (bars from bounce low to signal)")
    lines.append("| Variant | Median | Mean |")
    lines.append("|---|---|---|")
    for v, _ in VARIANTS:
        s = summarize(results[v])
        lines.append(f"| {v} | {s['median_bars_from_low']} "
                     f"| {s['mean_bars_from_low']} |")

    report = "\n".join(lines) + "\n"
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(report)
    return results, report


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--years", type=int, default=3)
    p.add_argument("--threshold", type=float, default=DEEP_WASHOUT_PCT)
    args = p.parse_args()
    try:
        with open("bot_config.json") as f:
            watchlist = json.load(f).get("universe", {}).get("core_watchlist", [])
    except (FileNotFoundError, json.JSONDecodeError):
        watchlist = []
    if not watchlist:
        print("No core_watchlist in bot_config.json.")
        return 1
    _, report = run(watchlist, years=args.years, threshold=args.threshold)
    print(report)
    print(f"Written: {REPORT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
