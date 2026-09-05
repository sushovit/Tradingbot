# Boardroom agenda — open questions requiring ratification

Items here are **not** implemented. They change trading policy and need a
decision; the evidence is recorded so the decision can be made on numbers.

---

## 1. Universe quality floor (raised 2026-08-20, from the BMNR approval)

**The premise needs correcting first.** The nominal "$25M floor" is not what
is configured. `bot_config.json` has:

```json
"universe": { "min_dollar_volume": 20000000 }   // $20M
```

BMNR passed the screen legitimately: **$23.5M average dollar volume**, above
the $20M floor that is actually in force. Nothing malfunctioned — the floor
is simply lower than the desk believed.

**The question**: should the floor rise, and should a liquidity floor alone
be the gate?

| Option | Effect |
|---|---|
| Leave at $20M | Status quo. Admits names like BMNR ($23.5M). |
| Raise to $25M | Excludes BMNR-class names. Nominal policy becomes real. |
| Raise to $30M | Materially narrows the universe; needs a re-run of the universe scan to size the impact. |

**Second question, harder**: liquidity is a *proxy*, not the concern. The
worry named was "digital-asset-treasury microcaps" — a **business-model**
risk (a company whose equity is a leveraged crypto wrapper), not a volume
risk. A $30M floor would exclude BMNR but would not exclude a $200M-volume
DAT name. If that is the real concern, the instrument is a **sector/business
exclusion list**, not a dollar-volume number.

**Evidence to gather before deciding** (not yet run): back-test expectancy
for candidates in the $20-30M ADV band versus above $30M. If the band is not
materially worse, raising the floor costs trades and buys nothing.

**Status**: awaiting ratification. No code change made.

---


---

## 2. Fractional bracket orders (researched 2026-09-02, work order item 3)

**Answer: NO.** Alpaca paper rejects every fractional order that is not a
*simple* order. Probed directly against our paper account on 2026-09-02
(symbol F, all accepted probes cancelled and cancellation verified; the
account finished with the same 3 positions and 3 bracket legs it started
with).

| Probe | Result |
|---|---|
| BRACKET + fractional qty, GTC | REJECTED — `fractional orders must be DAY orders` |
| BRACKET + fractional qty, DAY | REJECTED — `fractional orders must be simple orders` |
| BRACKET + notional $10, DAY | REJECTED — `fractional orders must be simple orders` |
| OTO + fractional qty, DAY | REJECTED — `fractional orders must be simple orders` |
| simple MARKET + fractional qty, GTC | REJECTED — `fractional orders must be DAY orders` |
| simple MARKET + fractional qty, DAY | **ACCEPTED** |
| simple LIMIT + fractional qty, DAY | **ACCEPTED** |

All rejections carry API code `42210000`.

**Constraints, stated plainly:**

1. Fractional quantities require `order_class = simple`. Bracket and OTO are
   both refused, so an attached stop leg is impossible on a fractional fill.
2. Fractional quantities require `time_in_force = DAY`. Our brackets use GTC,
   so even the TIF would have to change.
3. `notional` ordering does not route around either rule.

**Why this matters more than it looks.** The desk's core safety property is
that *exit orders live at the broker, not in our polling loop* — a crashed
or sleeping worker still has its stops. Adopting fractional sizing would mean
giving that up for those positions and managing their stops from the loop,
which is the failure mode the bracket architecture was built to remove (and
which the 2026-08-31 machine-sleep incident would have exposed).

So fractional shares are **not** an available fix for the `size_zero`
problem. On a $2,000 cap the binding constraints stay arithmetic: a 1% risk
budget of $20 cannot size a stop wider than $20/share, and the 25% notional
cap of $500 cannot buy one share above $500. The realistic levers are the
capital cap, the position cap, or accepting that wide-stop setups are
selected out — which is what the monthly `size_zero` table now measures.


---

## 3. SPY regime filter was measuring the wrong thing (found 2026-09-02)

**What was wrong.** The live filter computed a 20-period EMA of **five-minute**
SPY bars — roughly a 100-minute average — and blocked entries when SPY sat
below it. The backtest that justified the filter used the **20-day** EMA of
daily closes (`backtest.spy_regime_series`). Two different indicators. The
desk has been enforcing a regime nobody ever measured, and every "chop"
argument to date describes a different market state than the one the filter
was actually reacting to.

**Fixed** on branch `regime-completed-bar`: the regime is now the 20-day EMA
of daily closes on the last **completed** session, matching both the backtest
and the completed-bar semantics every daily signal already uses. The evidence
string (`spy_close`, `ema20d`, `regime`, `as_of`) is appended to every
`spy_bearish` pass and every `chop_reclaim` tag, so future rows can be
re-audited instead of trusted.

**Impact, reproducible via `python regime_audit.py`:**

- Regime blocks journaled: **30**
- Would NOT have been blocked under the corrected 20-day definition: **22** (73.3%)
- Still blocked: 8
- chop_reclaim exemptions taken: 13, of which 5 were actually in a TRENDING market — the exemption was not needed for those.

Flipped blocks by setup: trend_continuation 11, mean_reversion_reclaim 5, momentum_continuation 5, post_earnings_continuation 1
Flipped blocks by date: 2026-09-01 7, 2026-07-22 4, 2026-07-16 3, 2026-07-14 2, 2026-07-23 2, 2026-07-17 1, 2026-08-05 1, 2026-08-12 1, 2026-08-20 1

| Date | Ticker | Setup | SPY close | 20d EMA | as_of |
|---|---|---|---|---|---|
| 2026-07-14 | XOM | mean_reversion_reclaim | 749.13 | 745.27 | 2026-07-13 |
| 2026-07-14 | FIG | momentum_continuation | 749.13 | 745.27 | 2026-07-13 |
| 2026-07-16 | PYPL | momentum_continuation | 754.77 | 746.75 | 2026-07-15 |
| 2026-07-16 | CSCO | trend_continuation | 754.77 | 746.75 | 2026-07-15 |
| 2026-07-16 | IREN | trend_continuation | 754.77 | 746.75 | 2026-07-15 |
| 2026-07-17 | MRK | mean_reversion_reclaim | 750.87 | 747.14 | 2026-07-16 |
| 2026-07-22 | GM | mean_reversion_reclaim | 748.15 | 746.51 | 2026-07-21 |
| 2026-07-22 | NU | mean_reversion_reclaim | 748.15 | 746.51 | 2026-07-21 |
| 2026-07-22 | PATH | trend_continuation | 748.15 | 746.51 | 2026-07-21 |
| 2026-07-22 | PLTR | trend_continuation | 748.15 | 746.51 | 2026-07-21 |
| 2026-07-23 | T | momentum_continuation | 747.49 | 746.60 | 2026-07-22 |
| 2026-07-23 | NU | mean_reversion_reclaim | 747.49 | 746.60 | 2026-07-22 |
| 2026-08-05 | PLTR | momentum_continuation | 771.11 | 747.00 | 2026-08-04 |
| 2026-08-12 | AAL | trend_continuation | 770.52 | 756.50 | 2026-08-11 |
| 2026-08-20 | INTC | trend_continuation | 769.09 | 763.58 | 2026-08-19 |
| 2026-09-01 | TSLA | momentum_continuation | 766.87 | 765.34 | 2026-08-31 |
| 2026-09-01 | CRWD | post_earnings_continuation | 766.87 | 765.34 | 2026-08-31 |
| 2026-09-01 | INTC | trend_continuation | 766.87 | 765.34 | 2026-08-31 |
| 2026-09-01 | NOW | trend_continuation | 766.87 | 765.34 | 2026-08-31 |
| 2026-09-01 | F | trend_continuation | 766.87 | 765.34 | 2026-08-31 |
| 2026-09-01 | FDX | trend_continuation | 766.87 | 765.34 | 2026-08-31 |
| 2026-09-01 | AMAT | trend_continuation | 766.87 | 765.34 | 2026-08-31 |

**These are an UPPER BOUND on lost entries, not lost trades.** A flipped row only means the regime gate would have passed it. Each would still have had to clear the AI gatekeeper, the R:R >= 1.5 and notional rules, whole-share sizing on a $2,000 cap, the max-positions cap and the daily-loss breaker. On recent evidence those reject the large majority of what reaches them, so the realised number would be materially smaller.

**What this does not settle.** The 22 flipped blocks say the GATE was wrong,
not that the trades would have been good. It also means the Rule #5 chop
exemption for `mean_reversion_reclaim` was ratified on evidence about a
regime definition the desk was not running: 5 of the 13 exemptions it granted
were taken in markets that were actually TRENDING, where the exemption did
nothing. The exemption's value should be re-argued now that "chop" means what
the backtest meant by it.
---

## 4. Study: trend_continuation expectancy by ADX band (W3a)

**Question.** Does trend_continuation's edge exist at ADX 25-30, or only at ADX >= 30? 3y daily, 3.0R target, 62 trades from the core watchlist.

| ADX band | Trades | Win % | Avg R | Expectancy | PF | MaxDD (R) |
|---|---|---|---|---|---|---|
| ADX 25-30 | 44 | 31.8% | 0.13 | +0.130 | 1.16 | 10.33 |
| ADX >= 30 | 18 | 38.9% | 0.603 | +0.603 | 1.99 | 4.0 |

**Method.** The live desk runs adx_threshold 30 and the backtest base runs 28, so signals below 28 have never been generated and the 25-30 band cannot be read from live history. This study lowered its own threshold to 25 to admit the wider population, then bucketed by the ADX observed on each signal bar. `bot_config.json` was not modified. Reproduce with `python study_adx_band.py`.

_Measured on SIGNALS, not on trades the desk took. Every signal counted here would still have had to clear the AI gatekeeper, whole-share sizing on a $2,000 cap, the max-positions cap and the daily-loss breaker, so these counts are an UPPER BOUND on what would have been realised._
---

## 5. Study: reclaim and momentum expectancy by volume band (W3b)

**Question.** Is the 1.3x volume multiplier earning its keep, or would 1.0-1.3x signals have traded as well? 3y daily, 3.0R target.

| Setup | Volume band | Trades | Win % | Avg R | Expectancy | PF |
|---|---|---|---|---|---|---|
| mean_reversion_reclaim | 1.0-1.3x | 404 | 32.4% | 0.336 | +0.336 | 1.46 |
| mean_reversion_reclaim | >= 1.3x | 618 | 34.6% | 0.366 | +0.366 | 1.5 |
| momentum_continuation | 1.0-1.3x | 110 | 33.6% | 0.37 | +0.370 | 1.54 |
| momentum_continuation | >= 1.3x | 423 | 32.9% | 0.298 | +0.298 | 1.41 |

**Method.** The live config runs a 1.3x multiplier for both setups, so sub-1.3x signals were never generated and cannot be read from live history. This study lowered its own multiplier to 1.0 to admit them, then bucketed by the ratio on each signal bar (bar volume over its trailing 20-bar average). `bot_config.json` was not modified. Reproduce with `python study_volume_band.py`.

_Measured on SIGNALS, not on trades the desk took. Every signal counted here would still have had to clear the AI gatekeeper, whole-share sizing on a $2,000 cap, the max-positions cap and the daily-loss breaker, so these counts are an UPPER BOUND on what would have been realised._
---

## 6. Study: exit rule — breakeven floor vs pure ATR trail (W3c)

**Question.** Does the breakeven floor ratified 2026-09-03 improve expectancy, or does it just cut winners short? Same entries, same fill rules, 3y daily, 3.0R target, ATR x2.5.

| Setup | Exit rule | Trades | Win % | Avg R | Expectancy | PF | MaxDD (R) |
|---|---|---|---|---|---|---|---|
| trend_continuation | static stop | 31 | 38.7% | 0.431 | +0.431 | 1.57 | 7.97 |
| trend_continuation | ATR trail | 33 | 48.5% | 0.273 | +0.273 | 1.44 | 8.16 |
| trend_continuation | breakeven floor | 33 | 48.5% | 0.287 | +0.287 | 1.47 | 7.71 |
| momentum_continuation | static stop | 373 | 33.2% | 0.324 | +0.324 | 1.45 | 15.73 |
| momentum_continuation | ATR trail | 384 | 38.0% | 0.264 | +0.264 | 1.43 | 18.27 |
| momentum_continuation | breakeven floor | 384 | 36.2% | 0.27 | +0.270 | 1.46 | 20.95 |
| mean_reversion_reclaim | static stop | 785 | 34.4% | 0.379 | +0.379 | 1.53 | 18.0 |
| mean_reversion_reclaim | ATR trail | 841 | 39.1% | 0.333 | +0.333 | 1.54 | 16.97 |
| mean_reversion_reclaim | breakeven floor | 851 | 35.4% | 0.305 | +0.305 | 1.55 | 15.49 |

**Exit mix** (how each rule ends its trades):

| Setup | Exit rule | target | stop | gap_target | gap_stop |
|---|---|---|---|---|---|
| trend_continuation | static stop | 9 | 16 | 3 | 3 |
| trend_continuation | ATR trail | 6 | 19 | 2 | 6 |
| trend_continuation | breakeven floor | 6 | 20 | 2 | 5 |
| momentum_continuation | static stop | 100 | 204 | 24 | 45 |
| momentum_continuation | ATR trail | 76 | 233 | 23 | 52 |
| momentum_continuation | breakeven floor | 72 | 238 | 23 | 51 |
| mean_reversion_reclaim | static stop | 227 | 435 | 43 | 80 |
| mean_reversion_reclaim | ATR trail | 194 | 506 | 39 | 102 |
| mean_reversion_reclaim | breakeven floor | 175 | 548 | 35 | 93 |

**Method.** `backtest.py` could not answer this — `simulate_bracket` models a static stop and never trails, so neither exit rule had ever been simulated. `study_common.simulate_exit` adds the trail, with all three modes sharing identical fill conventions so the comparison isolates the stop and nothing else. The trail is recomputed from a bar's CLOSE and applies from the NEXT bar, so no trade exits on information its own bar had not yet produced; a bar spanning both levels is assumed to hit the stop first. Reproduce with `python study_exit_rule.py`.

_The 'static stop' row is the column reported in `backtest_report.md`, included here as the control._

_Measured on SIGNALS, not on trades the desk took. Every signal counted here would still have had to clear the AI gatekeeper, whole-share sizing on a $2,000 cap, the max-positions cap and the daily-loss breaker, so these counts are an UPPER BOUND on what would have been realised._
---

## 7. Study: capital cap — $2,000 vs $5,000 (W3d)

**Question.** How much does the $2,000 capital cap cost in entries, and would $5,000 recover them?

### Journal replay (as ordered) — n = 3

| Date | Ticker | Setup | Reason | Entry | Stop dist | Sizeable @ $2k | Sizeable @ $5k | Stop source |
|---|---|---|---|---|---|---|---|---|
| 2026-08-10 | SPCX | mean_reversion_reclaim | size_zero | 134.12 | — | — | — | unavailable |
| 2026-08-10 | PLTR | mean_reversion_reclaim | size_zero | 177.31 | — | — | — | unavailable |
| 2026-08-28 | CRM | mean_reversion_reclaim | size_zero | 253.76 | 22.20 | no | **yes** | reconstructed |

Of 3 journaled rows, **1** would have become sizeable at $5,000; 2 are indeterminate because the stop could not be recovered.

**This population is too small to decide on, and the reason is structural.** A `size_zero` row is only written when a signal has already cleared every other gate and then fails whole-share sizing, so the journal sees only the survivors of a narrow funnel. Enriched details (stop distance, risk budget) began on 2026-09-02, so older rows carry entry and equity alone and their stop had to be reconstructed by replaying the detector on that day's bars — possible only for symbols with cached history.

### Backtest replay (3y, all signals) — the usable population

| Setup | Signals | Sizeable @ $2k | Unlocked by $5k | Still blocked | % unlocked |
|---|---|---|---|---|---|
| trend_continuation | 34 | 34 | 0 | 0 | 0.0% |
| momentum_continuation | 527 | 509 | 13 | 5 | 2.5% |
| mean_reversion_reclaim | 1453 | 1418 | 25 | 10 | 1.7% |

**Unlocked entries by month** (the $5k-only column, spread over time):

| Month | mean_reversion_reclaim | momentum_continuation |
|---|---|---|
| 2023-11 | 1 | 0 |
| 2024-01 | 4 | 1 |
| 2024-02 | 2 | 0 |
| 2024-03 | 1 | 0 |
| 2024-05 | 2 | 2 |
| 2025-01 | 1 | 2 |
| 2025-10 | 1 | 1 |
| 2025-12 | 1 | 1 |
| 2026-01 | 3 | 2 |
| 2026-04 | 1 | 0 |
| 2026-05 | 5 | 2 |
| 2026-06 | 3 | 2 |

**Reading.** The `% unlocked` column is what raising the cap buys in entry COUNT. It says nothing about whether those entries are profitable — they are the trades the desk currently cannot afford, which skew toward wider stops and higher-priced names, not a random sample of the edge. Pair this with the expectancy studies before reading it as upside.

**Method.** Sizing uses the live rules (`risk.position_size`, 1% risk, 25% notional cap, whole shares). `bot_config.json` was not modified; the cap is passed as a parameter. Reproduce with `python study_capital_cap.py`.

_Measured on SIGNALS, not on trades the desk took. Every signal counted here would still have had to clear the AI gatekeeper, whole-share sizing on a $2,000 cap, the max-positions cap and the daily-loss breaker, so these counts are an UPPER BOUND on what would have been realised._
---

## 8. Study: day-one participation after a big up-day (W3e)

**Question.** After a >= 7% up-day at or near the 20-day high, does momentum_continuation get the desk on board the next session, and is that entry worth having? 3y daily over 44 names with such days.

| Qualifying up-days | Followed by a signal | Participation rate |
|---|---|---|
| 346 | 216 | 62.4% |

**Expectancy of the signals that did fire:**

| Trades | Win % | Avg R | Expectancy | PF | MaxDD (R) |
|---|---|---|---|---|---|
| 179 | 36.3% | 0.472 | +0.472 | 1.7 | 12.5 |

**Reading.** Participation is 62.4%: that is the share of big up-days the existing detector already converts into an entry the next session. The gap between that and 100% is the space a dedicated gap-and-go lane would occupy — but the missed days are missed because momentum_continuation's own filters (20-bar-high breakout, volume, +3% change) rejected them, so the residual is not free money; it is the set of pops those filters deliberately declined. Commissioning a lane is worth it only if a DIFFERENT thesis explains that residual, not merely the fact that it exists.

**Definitions.** Up-day = close-over-close >= 7%; near the high = close within 2% of the prior 20-bar high (a pop out of a downtrend base is excluded); next session = the pop bar is the signal bar and the fill is the following open. 3.0R target. Reproduce with `python study_day_one.py`.

_Measured on SIGNALS, not on trades the desk took. Every signal counted here would still have had to clear the AI gatekeeper, whole-share sizing on a $2,000 cap, the max-positions cap and the daily-loss breaker, so these counts are an UPPER BOUND on what would have been realised._

