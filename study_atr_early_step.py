"""
study_atr_early_step.py — S27 study: an ATR-based early step for wide-R
daily trades, against the live R/ATR ladder (S25). STUDY ONLY: reads the
bar caches in data/s27/ (daily + 5-minute IEX, fetched once from Alpaca) and
bot_config.json; writes nothing but its report.

    python study_atr_early_step.py [--out reports/s27_atr_early_step.md]

Population: live reclaim + momentum detectors (backtest.collect_signals with
the live volume multipliers, $5,000 sizing) on the 48-name core watchlist,
entries 2025-10-14 .. 2026-10-14. A signal is kept only if the same ticker
and setup did not fire in the previous 5 sessions (the detector repeats
while its condition holds); both rules then run on the SAME trades, each in
isolation.

Entry: the entry day's 09:30 5-minute open. Initial stop: the live rule
(position_mgmt.initial_stop - structural low - 0.25 ATR14, distance clamped
to 1.5-2.5 ATR). Target: entry + 3R. Exits on 5-minute bars: a bar opening
through the stop fills at the open, else at the stop; a bar spanning stop
and target is a stop; qualifying closes from 10:00 ET; a raised stop applies
from the next bar, never lowers, stays >= 0.1% under the close; live time
stops (day 5 close if the best move < +0.5R, day 10 close regardless).
Earnings exits are not modelled (no calendar history).

Rules compared:
  current  the live ladder: +1R -> entry - 0.25R, +1.5R -> entry,
           +2R -> entry + 1R, then highest daily close - 2.5 ATR
  early    the live ladder PLUS: +1.0 ATR -> entry - 0.5R,
           +1.5 ATR -> entry (whichever stop is higher)

ATR for the early steps is ATR14 as of the entry day (fixed for the trade).
"R > 1.5 ATR" excludes trades pinned at the 1.5-ATR clamp floor, i.e. the
wide-R trades (REGN 10-15: R 46.36 / ATR 19.99 = 2.32 ATR).
"""

import argparse
import json
import math
import pickle
import sys
from collections import Counter

import pandas as pd

import position_mgmt as pm

ET = "America/New_York"
CACHE = "data/s27"
RISK_USD = 50.0                   # 1% of $5,000 per R
DEDUP_SESSIONS = 5


def _cap(price, close, gap_pct):
    ceiling = math.floor(close * (1 - gap_pct / 100) * 100 + 1e-6) / 100
    return min(price, ceiling)


def simulate(bars, daily, entry_day, struct_stop, params, exits, early):
    """One trade. Returns dict or None (no data / invalid geometry)."""
    idx = bars.index.tz_convert(ET)
    days = sorted({d for d in idx.strftime("%Y-%m-%d") if d >= entry_day})[:10]
    if not days or days[0] != entry_day:
        return None
    sel = [d in set(days) for d in idx.strftime("%Y-%m-%d")]
    seg, sidx = bars[sel], idx[sel]
    entry = float(seg["open"].iloc[0])
    atr0 = pm.daily_atr(daily, int(params["atr_length"]), today=pd.Timestamp(entry_day).date())
    if not atr0:
        return None
    stop0 = pm.initial_stop(entry, struct_stop, atr0, params)
    if stop0 is None or stop0 >= entry:
        return None
    risk = entry - stop0
    target = entry + 3.0 * risk
    dcl = {str(t)[:10]: float(c) for t, c in zip(daily.index, daily["close"])}
    stop, hc, mfe_high = stop0, None, entry
    exit_px, why, steps = None, None, []
    day_cache = {}          # per session: (ATR14 as of the day, highest prior close)
    for n, (t, bar) in enumerate(zip(sidx, seg.itertuples())):
        o, h, l, c = bar.open, bar.high, bar.low, bar.close
        day = t.strftime("%Y-%m-%d")
        k = days.index(day) + 1
        if n > 0 and o <= stop:
            exit_px, why = o, ("stop" if stop < entry else "lock"); break
        if l <= stop:
            exit_px, why = stop, ("stop" if stop < entry else "lock"); break
        if n > 0 and o >= target:
            exit_px, why = o, "target"; break
        if h >= target:
            exit_px, why = target, "target"; break
        mfe_high = max(mfe_high, h)
        hhmm = t.strftime("%H:%M")
        if hhmm >= params["not_before_et"]:
            hc = c if hc is None else max(hc, c)
            if day not in day_cache:
                prior = [v for d, v in dcl.items() if entry_day <= d < day]
                day_cache[day] = (pm.daily_atr(daily, int(params["atr_length"]),
                                               today=pd.Timestamp(day).date()),
                                  max(prior) if prior else None)
            atr_t, hdc = day_cache[day]
            cand, step = pm.r_atr_target(entry, stop0, hc, hdc, atr_t, c, params)
            cand = cand or 0.0
            if early:
                moved = (hc - entry) / atr0
                e = entry if moved >= 1.5 else (entry - 0.5 * risk if moved >= 1.0 else None)
                if e is not None:
                    e = _cap(math.ceil(e * 100 - 1e-6) / 100, c, params["market_gap_pct"])
                    if e > cand:
                        cand, step = e, ("+1.5ATR" if moved >= 1.5 else "+1.0ATR")
            if cand > stop:
                stop = cand
                steps.append(step)
        mfe_r = (mfe_high - entry) / risk
        if hhmm == exits["exit_at_et"]:
            if k >= exits["max_hold_days"]:
                exit_px, why = c, "time (day 10)"; break
            if k == exits["time_stop_day"] and mfe_r < exits["time_stop_min_mfe_r"]:
                exit_px, why = c, "time (day 5)"; break
    if exit_px is None:
        return None                                   # still open at data end
    r = (exit_px - entry) / risk
    mfe_r = max(0.0, (mfe_high - entry) / risk)
    return {"entry": entry, "risk": risk, "r_atr": risk / atr0, "r": r,
            "mfe_r": mfe_r, "giveback_r": max(0.0, mfe_r - r), "exit": why,
            "first_step": steps[0] if steps else None}


def summary(trades):
    if not trades:
        return {"n": 0}
    rs = [t["r"] for t in trades]
    peaks = [t for t in trades if t["mfe_r"] > 0]
    went_1atr = [t for t in trades if t["mfe_r"] * t["r_atr"] >= 1.0]
    return {
        "n": len(rs),
        "win": 100.0 * sum(r > 0 for r in rs) / len(rs),
        "exp": sum(rs) / len(rs),
        "total": sum(rs),
        "giveback_all": sum(t["giveback_r"] for t in trades) / len(trades),
        "giveback_1atr": (sum(t["giveback_r"] for t in went_1atr) / len(went_1atr)
                          if went_1atr else None),
        "n_1atr": len(went_1atr),
        "capture": (100.0 * sum(t["r"] for t in peaks) / sum(t["mfe_r"] for t in peaks)
                    if peaks and sum(t["mfe_r"] for t in peaks) else None),
        "exits": Counter(t["exit"] for t in trades),
    }


def report():
    cfg = json.load(open("bot_config.json"))
    prof = cfg["risk_profiles"]["Moderate"]
    params, exits = pm.r_atr_params(prof), pm.daily_exit_params(prof)
    daily = pickle.load(open(f"{CACHE}/daily.pkl", "rb"))
    five = pickle.load(open(f"{CACHE}/five.pkl", "rb"))
    sigs = pickle.load(open(f"{CACHE}/signals.pkl", "rb"))

    kept, last = [], {}
    for s in sorted(sigs, key=lambda s: (s["symbol"], s["strategy"], s["entry_day"])):
        key = (s["symbol"], s["strategy"])
        dates = [str(t)[:10] for t in daily[s["symbol"]].index]
        pos = dates.index(s["entry_day"]) if s["entry_day"] in dates else None
        if pos is None:
            continue
        if key in last and pos - last[key] < DEDUP_SESSIONS:
            continue
        last[key] = pos
        kept.append(s)

    rows = []
    for s in kept:
        b = five.get(s["symbol"])
        if b is None:
            continue
        idx = b.index.tz_convert(ET)
        mins = idx.hour * 60 + idx.minute
        rth = b[(mins >= 570) & (mins < 960)]
        cur = simulate(rth, daily[s["symbol"]], s["entry_day"], s["struct_stop"],
                       params, exits, early=False)
        ear = simulate(rth, daily[s["symbol"]], s["entry_day"], s["struct_stop"],
                       params, exits, early=True)
        if cur and ear:
            rows.append((s, cur, ear))

    pickle.dump(rows, open(f"{CACHE}/rows.pkl", "wb"))
    L = []
    P = L.append
    # Cent rounding puts trades pinned at the 1.5-ATR clamp floor at
    # 1.50-1.51, so "wide" starts above 1.55.
    floor = [(s, c, e) for s, c, e in rows if c["r_atr"] <= 1.55]
    wide = [(s, c, e) for s, c, e in rows if c["r_atr"] > 1.55]
    regn = [(s, c, e) for s, c, e in rows if c["r_atr"] >= 2.0]
    P(f"Signals 2025-10-14..2026-10-14: {len(sigs)} raw, {len(kept)} after the "
      f"5-session de-duplication, {len(rows)} resolved trades simulated "
      f"(both rules). R/ATR: {len(floor)} at the 1.5-ATR clamp floor "
      f"(<= 1.55), {len(wide)} wider (> 1.55), of which {len(regn)} >= 2.0 "
      f"(REGN 10-15 was 2.32).")
    P("")
    for label, pop in (("R > 1.55 ATR - wide-R trades", wide),
                       ("R >= 2.0 ATR - REGN-like", regn),
                       ("R at the 1.5-ATR floor", floor),
                       ("All trades", rows)):
        a, b = summary([c for _, c, _ in pop]), summary([e for _, _, e in pop])
        P(f"### {label} — n = {a['n']}")
        P("")
        P("| Rule | Win % | Expectancy (R) | Total R | $ @ $50/R | Avg give-back (R), all | Avg give-back (R), trades that went >= +1 ATR | Capture % |")
        P("|---|---|---|---|---|---|---|---|")
        for name, m in (("current ladder", a), ("+ ATR early steps", b)):
            P(f"| {name} | {m['win']:.1f}% | {m['exp']:+.3f} | {m['total']:+.1f} | "
              f"{m['total'] * RISK_USD:+,.0f} | {m['giveback_all']:.2f} | "
              f"{m['giveback_1atr']:.2f} (n={m['n_1atr']}) | {m['capture']:.1f}% |")
        P("")
        P(f"Exits — current: {dict(a['exits'])}; early: {dict(b['exits'])}")
        diff = [(s, c, e) for s, c, e in pop if abs(c["r"] - e["r"]) > 1e-9]
        if diff:
            better = sum(e["r"] > c["r"] for _, c, e in diff)
            P(f"Trades whose result changed: {len(diff)} ({better} better, "
              f"{len(diff) - better} worse); mean change "
              f"{sum(e['r'] - c['r'] for _, c, e in diff) / len(diff):+.3f}R per "
              f"changed trade.")
        P("")
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    a = ap.parse_args()
    text = report()
    print(text)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(text + "\n")
    sys.exit(0)
