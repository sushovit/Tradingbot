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


---

## 9. Ratifications and ops items (log, not a study)

**CRCL re-ratified 2026-09-04 (owner, on PM recommendation): HOLD.**
Thesis (washout reclaim) intact after 10 sessions; stop at entry (90.32,
breakeven floor), target 122.13 unchanged. Reviewer: no further flag
needed unless price closes below 90.32 or the target is hit.

**Open ops items for the 2026-09-17 boardroom** (no code change before then):
- API credit pre-flight at worker start (same shape as the Ollama pre-flight)
  plus billing auto-reload. 2026-09-08: credits ran out at 09:36 ET, four
  signals (FCX, XOM, SMCI, BE) reached the gatekeeper and got errors instead
  of verdicts; the desk failed closed as designed.
- Review-prompt fact (d): shadow error baseline is ~38%, not ~11%.
- Daily setups fail closed for a symbol whose partial daily bar never
  arrives (W6 consequence). Decide whether a fallback deadline is wanted.
- Scheduled auto-start of run_worker.py at 19:10 Nepal on trading days,
  gated by a holiday calendar (broker.trading.get_clock()).
- Mid-day `outside_hours` rejections (2026-09-08: LRCX, ORCL, KLAC, QCOM,
  PG at 12:02–12:27 ET) — the time-window filter is the least-studied rule
  in the file; consider a study before the 11:30–14:00 block is ratified
  again.

**2026-09-08 ops incident.** DNS drop at 14:42 ET -> daily bars unavailable -> CRCL/SLB stops ratcheted on 5-min ATR to 0.5% under price (W7, fixed 09-09). SLB exited 57.10 (+$6.20) on that stop; CRCL's stop stands at 95.80. SLB's exit and CRCL's eventual stop-out are both ops-incident — exclude from the exit-rule comparison.

**Owner decision 2026-09-09 on CRCL: leave the 95.80 stop as is.** No manual adjustment. The ratchet is monotonic, so the stop will not widen on its own; CRCL will exit at 95.80 (+$5.48/sh on a 90.32 entry) unless the 122.13 target is reached first.

---

## 10. PROPOSAL for the boardroom of Saturday 2026-09-19 (PM draft, 2026-09-18)

Status: **RATIFIED 2026-09-20 by the PM on the owner's delegation (09-10),
with these exceptions: 10.6 (exit rule) HELD — breakeven floor stays until
the owner and PM discuss; 10.1 cap set in config but effective only on the
paper-account reset, which waits for SPCX/SWKS to exit naturally (no manual
close).** Ratified items deploy Sunday 09-20 after tests; live Monday
09-21.

**Amended 2026-09-21: the 10.6 hold is LIFTED.** The owner ratified the exit
rule as merged. 10.1 remains conditional on the paper reset. See 10.6. Stats split at 09-21. Each item: what / evidence / change / how we'd
know it was wrong.

### The week's evidence (2026-09-08 → 09-18, corrected system)

Nine sessions since the regime, bar, entry-price and ratchet fixes. Regime
read **chop every day** (SPY below its 20-day EMA all week), so continuation
setups were blocked and only `mean_reversion_reclaim` could fire.

| | Count |
|---|---|
| Reclaims that reached the gatekeeper | 27 |
| Approved | 3 (ARM 74 → size_zero; SWKS 74 → bought; SPCX 78 → bought after a restart re-ask; first ask was 68) |
| Rejected citing ADX < 20/25 or RSI < 45/50 ("ranging market") | **19 of 24** |
| Rejected for RSI > 75 (correct: overextended) | 2 (KLAC, LRCX) |
| Errors (credits, 09-08) | 4 signals |
| Intraday `adx_low` rejections | 937 (80 in the 25–30 band, 19 in 28–30) |
| `size_zero` | 1 (ARM: stop $15.27 vs budget $14.85) |
| Late starts (> 09:35 ET) | 5 of 9 |
| Second worker started by hand | 2 (09-11, 09-17 — the 09-17 one traded) |
| Network events (laptop Wi-Fi) | 6 sessions; Alpaca-side outage 1 (09-11, 46 min) |

Open: SWKS 1 @ 89.86 (stop 74.94 / 127.54), SPCX 1 @ 154.25 (stop 144.44 /
183.06). A-book realized −$20.65 over 13 exits. Cash $1,982.

### Decisions proposed

**10.1 Capital cap → $5,000.** Owner would fund $5k live (09-10). Change:
`capital_cap_usd: 5000`; reset the Alpaca paper account to $5,000 cash
**while flat or accept that reset wipes broker positions** — do it Sunday
after closing SWKS/SPCX by hand if they are still open, or wait for them to
exit (owner's call; PM recommends waiting for natural exits and resetting the
following weekend if needed, since the cap change alone does nothing until
broker equity exceeds $2,000). Also `universe.max_price: 1200` (25% of $5k).
Wrong if: nothing — this is a mirror of the live intent, not a bet.

S4 landed 2026-09-21: `capital_cap_usd` 5000, `universe.max_price` 1200,
Moderate `risk_per_trade_pct` 1.0, `max_positions` 4, `daily_loss_limit_pct`
4.0, plus a minimum-lot tolerance (`min_lot_tolerance: 0.15`) — when one
whole share's risk exceeds the budget by no more than 15%, size 1 share and
journal `risk_pct_actual` on the BUY row. **The paper-account reset to
$5,000 is the OWNER's action at Alpaca, taken while flat.** Until it
happens the cap is inert: `effective_equity` is the MIN of broker equity
and the cap, so at $1,977 of paper equity sizing is unchanged. What bites
on 09-21 is `max_positions` 3 → 4, the 4% daily breaker, and the risk
percent — the ARM signal journaled `size_zero` this week (stop $15.27 vs a
$14.85 budget) sizes 1 share at today's $1,977 simply because 0.75% → 1.0%
raises the budget to $19.78, and 3 shares once the reset lands. The
tolerance is not what unlocks ARM; it covers the band above the floor that
the cap only moves.

**10.2 Gatekeeper prompt: setup-specific criteria for reclaims (the big one).**
Evidence: 19 of 24 reclaim rejections this week cited low ADX or sub-45 RSI.
A washout-and-reclaim *is* a low-ADX, depressed-RSI pattern by construction;
the prompt is grading it with trend-continuation rules. The 3-year study
says reclaim is the desk's best setup (+0.37R, 785 trades) and the live gate
is admitting ~1 in 9. Change (`prompts.py`): a per-setup rubric block for
`mean_reversion_reclaim` — ADX is *not* a rejection criterion; RSI must have
turned up from its low (rising over the last 3 bars) rather than exceed 45;
reclaim-bar volume ≥ prior average; close above the reclaim level; no
earnings inside 5 sessions. Keep RSI > 75 as an over-extension reject.
Trend/momentum rubric unchanged. Journal `prompt_version` on every verdict.
Wrong if: reclaim approval rate rises but the 20-trade probation for the
new rubric shows expectancy below +0.2R. Run as **probation** (one open
reclaim at a time until 20 trades under the new rubric).

**10.3 Persist the gatekeeper's per-bar cache; harden single-instance.**
Evidence: SPCX 09-17 — first ask 68 (blocked), restart, re-ask 78 (bought).
Change: cache `(ticker, setup, bar)` rejections in the journal, not memory;
`run_worker.py` refuses a second start if `bot.run` exists and the PID is
alive, regardless of heartbeat age. Wrong if: a legitimate crash-restart
gets refused — the watchdog path uses `--force-takeover` and is unaffected.

**10.4 Momentum volume multiplier 1.3 → 1.0; reclaim stays 1.3.** Evidence:
item 5 (momentum 1.0–1.3×: +0.37R vs ≥1.3×: +0.30R; reclaim the reverse).
Change: `volume_multipliers.momentum_continuation: 1.0`. Wrong if: momentum
expectancy over the next 30 signals is below +0.25R.

**10.5 ADX threshold: keep 30.** Evidence: item 4 (25–30: +0.13R, PF 1.16;
≥30: +0.60R, PF 1.99). This week's 80 signals in the 25–30 band are the
band the study says not to trade. No change.

**10.6 Exit rule: static structural stop, no trail, no floor** — for the
daily setups only. Evidence: item 6 (static beats ATR trail beats floor on
expectancy for all three setups; floor lowers MaxDD on reclaim only).
Live evidence: SLB and CRCL were both scratched by a 5-min-ATR ratchet
(ops incident) — no live trade has yet reached its 3R target under any
rule. Change: `trailing_stop_type: "none"` for daily setups; intraday
`trend_continuation` keeps the ATR trail + floor (the study did not model
intraday). Wrong if: reclaim MaxDD over the next 30 trades exceeds 12R.
**PM flags this as the one to argue about** — it trades smoothness for
expectancy, and the owner's tolerance for a −10% open position (SWKS on
09-14) is the real input.

**Status: RATIFIED 2026-09-21 by the OWNER, as merged.** Daily setups trail
nothing — the structural stop and the target stand for the life of the
trade. Intraday keeps the ATR trail and the +1R breakeven floor.

**Process note, recorded because the agenda is the governance record:** this
was implemented and merged on 2026-09-21 as S6 (`221c040`, merged
`996ff80`) while 10.6 was still marked HELD, on a work order that did not
reference the hold. The code and the ratification agree, and the owner has
ratified it as merged, so nothing is being reverted — but the merge preceded
the decision rather than following it, and the ledger should say so.

What landed: `position_mgmt.trailing_type_for()` resolves a position's rule
from its timeframe; daily reads the profile's new `daily_trailing` key, set
to `"none"` on both Aggressive and Moderate. `maybe_ratchet_stop` returns
False immediately for type `"none"`. `daily_trailing` falls back to
`trailing_stop_type` and an absent `timeframe` reads as intraday, so
profiles and positions that predate the change keep the behaviour they had.

**Effective immediately on the open positions.** SPCX (entry 154.25, stop
144.445) and SWKS (entry 89.86, stop 74.94) are both `timeframe: daily` and
neither had reached +1R, so neither had begun trailing. Their stops are now
fixed at those levels until the target or the stop fills. The rule reaches
positions opened under the previous one.

**Wrong if:** reclaim MaxDD over the next 30 trades exceeds 12R — unchanged
from the proposal, and now the live test of it.

**10.7 Sizing at $5,000: Moderate 0.75% → 1.0%; max_positions 3 → 4;
daily-loss breaker 3% → 4%.** Evidence: ARM size_zero (stop $15.27 vs $14.85
budget); at $5k and 1.0% the budget is $50. 4 positions × 1% = 4% max open
risk, so the breaker must move with it or it binds at 3 positions. Wrong if:
any single day's realized loss exceeds 4% — the breaker then did its job and
the setting stands; if it happens twice in a month, revert to 3/3%.

**10.8 Minimum-lot tolerance.** Change (`risk.position_size`): if 1 share's
risk exceeds the budget by ≤ 15%, take 1 share and journal `risk_pct_actual`.
Mostly moot at $5k, cheap, keeps the ARM case from recurring on high-priced
names. Wrong if: journaled actual risk ever exceeds 1.15× budget (test).

**10.9 Soft volume band → gatekeeper.** Signals failing volume in the
1.0–1.3× band go to Claude with the shortfall stated, instead of dying.
Reclaim only (momentum is covered by 10.4). Wrong if: approval rate on
soft-band reclaims exceeds the hard-band rate — then the gate isn't
discriminating and the band should close.

**10.10 Conviction-scaled sizing — PROBATION, not ratified.** 70–79 base
risk, 80+ at 1.25× base. No study; run it as a probation lane of 20 trades
with the multiplier journaled, decide on the sample.

**10.11 Rejection-outcome tracker — COMMISSION.** Nightly job: for every
signal rejected today (filter or gatekeeper), replay its bracket forward
for 10 sessions and journal target/stop/neither against the rejection
reason. Report: rejections × outcome by filter, monthly. This is how every
future boardroom decides which gate is earning its keep. No policy change.

S3 landed ee0633e; first table 2026-09-20; coverage 7.7% — deterministic
rejections carry no geometry (S11, next weekend).

**10.12 Crypto-beta class: measure, don't exclude.** Sector tag exists; 3
trades tagged. Revisit at 20 trades. No change.

**10.13 Non-trend research lanes — backtest only.** Commission two
`backtest.py` lanes: oversold-bounce (RSI < 30 → first close above EMA9) and
base-breakout (20-day range < 8% → close above range high). Report by the
next boardroom; nothing goes live from this item.

**10.14 Ops (all small; all Sunday):** API-credit pre-flight + billing
auto-reload; review prompt facts (shadow baseline ~38%; floor rule;
restart artifacts); scheduled auto-start at 19:10 Nepal weekdays gated by
`get_clock()`; W8 stale stop-id reconcile; Wi-Fi adapter power management
off (owner, by hand); worker must never be started twice (10.3).

**10.15 Host (Phase 2, not this weekend):** VPS before go-live. The
laptop's Wi-Fi produced timeouts in 6 of 9 sessions; a live desk cannot
run on it. Scope in the next plan.

### Not proposed
Prediction model (12 labelled outcomes); lowering `claude_conviction_threshold`
(the histogram is bimodal, nothing lives at 60–69); a mobile/Termux host.

---

## 11. RESEARCH RESULT — non-trend lanes (S9, 2026-09-21)

Commissioned by item 10.13. Two lanes added to `backtest.py` behind the
research flag. **Neither is wired live and neither can be:** no entry in
`RESEARCH_STATUS`, no `strategies/` module, no config key. The live loop
dispatches through `strategies.REGISTRY`, which does not contain them.

Universe: the 48 core-watchlist names with cached daily bars, 2023-07-12 to
2026-07-24. Mechanics identical to the live playbook — next-bar-open entry,
bracket exit, whole shares, 1% risk on $2,000, 25% position cap, one
position per symbol at a time. Regime from SPY vs its 20-day EMA.

**oversold_bounce** — RSI14 below 30, then the first close back above EMA9.
Stop at that bounce bar's low, target 3R.

| Regime | Trades | Win% | Avg R | Expectancy (R) | PF | MaxDD (R) | Avg $ |
|---|---|---|---|---|---|---|---|
| trending | 217 | 27.2 | 0.091 | 0.091 | 1.12 | 13.49 | 1.16 |
| chop | 128 | 39.8 | 0.574 | 0.574 | 1.85 | 12.01 | 10.02 |
| **all** | **345** | **31.9** | **0.270** | **0.270** | **1.37** | **15.47** | **4.45** |

**base_breakout** — 20-day range under 8% of price, then a close above the
range high on >= 1.3x the base's average volume. Stop at the range low,
target 3R.

| Regime | Trades | Win% | Avg R | Expectancy (R) | PF | MaxDD (R) | Avg $ |
|---|---|---|---|---|---|---|---|
| trending | 57 | 38.6 | 0.543 | 0.543 | 1.85 | 7.01 | 9.35 |
| chop | 11 | 36.4 | 0.483 | 0.483 | 1.75 | 4.00 | 9.34 |
| **all** | **68** | **38.2** | **0.533** | **0.533** | **1.83** | **8.00** | **9.35** |

### Target-R sensitivity

| Target | Setup | Trades | Win% | Expectancy (R) | PF |
|---|---|---|---|---|---|
| 2R | oversold_bounce | 359 | 39.8 | 0.209 | 1.32 |
| 3R | oversold_bounce | 345 | 31.9 | 0.270 | 1.37 |
| 4R | oversold_bounce | 340 | 26.5 | 0.306 | 1.38 |
| 2R | base_breakout | 73 | 42.5 | 0.304 | 1.51 |
| 3R | base_breakout | 68 | 38.2 | 0.533 | 1.83 |
| 4R | base_breakout | 61 | 27.9 | 0.358 | 1.47 |

### Robustness

| | oversold_bounce | base_breakout |
|---|---|---|
| Trades / year | ~115 | ~23 |
| Symbols producing a trade | 48 of 48 | 31 of 48 |
| Total R | 93.3 | 36.3 |
| Top 5 trades as % of total R | 27% | **47%** |
| Median R | -1.000 | -1.000 |
| Exits (stop / target / gap) | 209 / 88 / 48 | 36 / 21 / 11 |

**Median R is -1.000 for both, and that is not a defect.** A 3R bracket
loses small most of the time and is carried by its winners; the mean is the
number that matters and the median is only there to say that the mean is not
describing a typical trade. It is recorded because a 2026-09-20 study was
nearly misread the other way, on an 8-cent stop.

### What the numbers say

**1. The commission found what it was looking for, in oversold_bounce.** It
earns +0.574R in CHOP against +0.091R trending — PF 1.85 versus 1.12. That
is the opposite polarity to everything the desk runs, and chop is precisely
when the SPY filter switches the rest of the book off. Last week the regime
read chop nine sessions out of nine and only one setup could fire.

**2. Its trending half is not worth having.** +0.091R over 217 trades at
PF 1.12 is noise with commission risk attached. If this ever goes live it
goes live chop-only, which would make it the first setup the desk runs
*because* of the regime rather than in spite of it.

**3. base_breakout looks better and is weaker.** +0.533R overall beats
reclaim's +0.37R, but 47% of its total R comes from 5 trades out of 68, it
fires ~23 times a year across 48 names, and 17 of the 48 never produce one.
Its 3R peak sits between a lower 2R and a much lower 4R, which is the shape
of a sample too small to have a peak. Not actionable on this evidence.

**4. oversold_bounce wants a wider target, base_breakout does not.**
Expectancy rises monotonically 2R -> 4R for the bounce (0.209 -> 0.306),
which is a drift payoff. The breakout's collapse at 4R is on 61 trades and
should be read as noise, not as a target ruling.

### Recommendation (PM, for the boardroom — nothing is requested now)

Neither lane goes live from this item, per 10.13. The question worth putting
to the next boardroom is narrower than the commission: **should
oversold_bounce be developed as a chop-only lane?** That would need a
deliberate design decision the desk has never made — `spy_filter_exempt`
currently means "ignore the regime", and this would need "require chop",
which no config key expresses today. base_breakout should be re-measured
after another year of bars before it is discussed at all.
---

## 12. Profit lock for daily setups (S15 study, 2026-10-09)

**CEO decision 2026-10-09:** agenda 10.6 (static stops for daily setups) is reversed; daily positions get a profit lock (S16). This item is the evidence.

### Study (S15)

**Question.** Which profit lock should replace the static stop (10.6) for daily setups? Same entries per setup, same fill rules, 3y daily, 3R target.

| Setup | Variant | Trades | Win % | Avg R | Expectancy | PF | MaxDD (R) | Hit +1.5R, closed at a loss |
|---|---|---|---|---|---|---|---|---|
| momentum_continuation | a static | 373 | 33.2% | 0.324 | +0.324 | 1.45 | 15.73 | 11.5% |
| momentum_continuation | b BE at +1R | 382 | 27.0% | 0.267 | +0.267 | 1.46 | 22.55 | 5.5% |
| momentum_continuation | c BE at +1.5R | 379 | 28.2% | 0.241 | +0.241 | 1.37 | 20.08 | 6.3% |
| momentum_continuation | d +2R -> +1R | 377 | 36.9% | 0.292 | +0.292 | 1.43 | 15.73 | 8.0% |
| momentum_continuation | e +1.5R->+0.5R, +2.5R->+1.5R | 382 | 39.0% | 0.214 | +0.214 | 1.33 | 15.51 | 5.5% |
| momentum_continuation | f BE at +1R, +2R->+1R | 384 | 30.5% | 0.249 | +0.249 | 1.43 | 20.07 | 4.4% |
| mean_reversion_reclaim | a static | 1022 | 33.8% | 0.354 | +0.354 | 1.49 | 23.48 | 15.0% |
| mean_reversion_reclaim | b BE at +1R | 1095 | 27.5% | 0.312 | +0.312 | 1.55 | 20.77 | 4.7% |
| mean_reversion_reclaim | c BE at +1.5R | 1064 | 30.3% | 0.335 | +0.335 | 1.52 | 19.11 | 8.4% |
| mean_reversion_reclaim | d +2R -> +1R | 1069 | 37.7% | 0.342 | +0.342 | 1.5 | 19.7 | 11.0% |
| mean_reversion_reclaim | e +1.5R->+0.5R, +2.5R->+1.5R | 1093 | 42.0% | 0.321 | +0.321 | 1.51 | 24.21 | 6.9% |
| mean_reversion_reclaim | f BE at +1R, +2R->+1R | 1119 | 32.3% | 0.312 | +0.312 | 1.56 | 18.28 | 3.8% |

**Exit mix:**

| Setup | Variant | target | stop at a loss | stop at >= 0R (locked) |
|---|---|---|---|---|
| momentum_continuation | a static | 124 | 249 | 0 |
| momentum_continuation | b BE at +1R | 103 | 215 | 64 |
| momentum_continuation | c BE at +1.5R | 107 | 235 | 37 |
| momentum_continuation | d +2R -> +1R | 107 | 247 | 23 |
| momentum_continuation | e +1.5R->+0.5R, +2.5R->+1.5R | 93 | 242 | 47 |
| momentum_continuation | f BE at +1R, +2R->+1R | 94 | 219 | 71 |
| mean_reversion_reclaim | a static | 345 | 677 | 0 |
| mean_reversion_reclaim | b BE at +1R | 301 | 587 | 207 |
| mean_reversion_reclaim | c BE at +1.5R | 322 | 634 | 108 |
| mean_reversion_reclaim | d +2R -> +1R | 310 | 680 | 79 |
| mean_reversion_reclaim | e +1.5R->+0.5R, +2.5R->+1.5R | 288 | 664 | 141 |
| mean_reversion_reclaim | f BE at +1R, +2R->+1R | 278 | 599 | 242 |

**Method.** `study_common.simulate_lock`: the step is decided on a bar's CLOSE and applies from the NEXT bar; a stop never moves down; a bar spanning stop and target hits the stop first; gaps fill at the open. One position per symbol at a time. The live rule will trigger on the last completed 5-minute close, which reacts sooner than a daily close, so these rows are a conservative read of how often each lock engages. Reproduce with `python study_profit_lock.py`.

_Measured on SIGNALS, not on trades the desk took. Every signal counted here would still have had to clear the AI gatekeeper, whole-share sizing on a $2,000 cap, the max-positions cap and the daily-loss breaker, so these counts are an UPPER BOUND on what would have been realised._

### Live record (journal, 2026-10-09)

The CEO decision cites 19 exits. The journal holds **16 SELL rows / 14
position closes** (NOK and ORCL 2026-08-13 are the confirmed double fills,
two rows each); no other table records exits. The 16 rows as journaled:

| Date | Ticker | Setup | Entry | Exit | P&L $ | R | How it closed |
|---|---|---|---|---|---|---|---|
| 07-07 | SPCX | CEO event_flow | 164.21 | 157.82 | -6.39 | n/a | Stop Loss (synced) |
| 07-10 | MRVL | CEO mean_reversion_reclaim | 239.19 | 235.91 | -3.28 | n/a | Stop Loss (synced) |
| 07-13 | FCX | CEO mean_reversion_reclaim | 60.65 | 59.75 | -1.80 | n/a | Stop Loss (synced) |
| 07-15 | DELL | CEO momentum_continuation | 460.99 | 433.29 | -27.70 | n/a | Stop Loss (synced) |
| 08-10 | UBER | mean_reversion_reclaim | 77.135 | 77.40 | +0.53 | +0.05 | Stop Loss |
| 08-11 | PLTR | CEO momentum_continuation | 176.15 | 172.875 | -6.55 | n/a | Stop Loss (synced) |
| 08-13 | ORCL | mean_reversion_reclaim | 157.81 | 156.16 | -1.72 | -0.17 | Stop Loss |
| 08-13 | ORCL | mean_reversion_reclaim | 157.81 | 156.16 | -1.72 | -0.17 | Stop Loss (double fill) |
| 08-13 | NOK | mean_reversion_reclaim | 10.76 | 10.74 | +0.14 | -0.04 | Stop Loss |
| 08-13 | NOK | mean_reversion_reclaim | 10.76 | 10.74 | +0.14 | -0.04 | Stop Loss (double fill) |
| 08-21 | BAC | CEO momentum_continuation | 61.78 | 61.78 | +1.54 | n/a | Stop Loss (resolved) |
| 09-01 | BMNR | momentum_continuation | 21.71 | 23.49 | +7.90 | +0.53 | Stop Loss (resolved) |
| 09-08 | SLB | mean_reversion_reclaim | 55.86 | 57.10 | +6.20 | +0.45 | Stop Loss (resolved) |
| 09-09 | CRCL | mean_reversion_reclaim | 90.32 | 95.83 | +5.51 | +0.51 | Stop Loss (synced) |
| 10-07 | ON | momentum_continuation | 84.75 | 82.812 | -9.69 | -1.09 | Stop Loss (synced) |
| 10-09 | SWKS | mean_reversion_reclaim | 89.86 | 74.63 | -15.23 | -1.02 | Stop Loss |

R is (exit - entry) / (entry - stop) from the BUY's gatekeeper context;
"n/a" where the BUY was a CEO order sheet with no stop in a decision row.

What the rows show:
- **0 targets hit.** Every one of the 16 exits is a stop.
- **Every profitable exit was a raised stop** (UBER, BMNR, SLB, CRCL, and
  BAC at breakeven): the stop had been trailed above entry before it filled.
- **Since 10.6 (2026-09-21): two exits, both full-stop losses** — ON -1.09R,
  SWKS -1.02R. Open: SPCX (+0.9R, stop 144.445, $10 below entry) and PLTR.
- **Ledger oddities, not acted on:** NOK shows +$0.14 P&L on an exit below
  entry, and BAC +$1.54 on an exit equal to entry — those rows' P&L was
  computed against a different (corrected) entry than the one now stored.

### Reading

- **No variant beats the static stop on expectancy** in the 3y backtest:
  momentum +0.324R static vs +0.214R to +0.292R with a lock; reclaim +0.354R
  vs +0.312R to +0.342R. The live record (0 of 16 at target) is the
  opposite of the backtest's ~33% target rate; that gap is the open question,
  not the lock.
- **Locks cut the give-back.** Trades that reached +1.5R and still closed at
  a loss: momentum 11.5% static -> 5.5% with (b) or (e); reclaim 15.0% ->
  4.7% (b) / 6.9% (e).
- **(d) +2R -> +1R is closest to static on expectancy** for both setups
  (-0.03R momentum, -0.01R reclaim) while still locking 1R on the trades
  that run, and keeps MaxDD at or below static.
- **(e), the placeholder,** has the highest win rate (39% / 42%) and the
  lowest give-back for momentum, at the largest expectancy cost for momentum
  (-0.11R).

The CEO sets the final step values; S16 ships them as config.

### Variant f and the decision (2026-10-09)

Variant **f** (BE at +1R, then +2R -> +1R) was added on the CEO's fixed
rule: ship f only if its expectancy >= b's on BOTH setups AND its give-back
<= b's on both; otherwise ship b.

| Setup | b expectancy | f expectancy | f >= b | b give-back | f give-back | f <= b |
|---|---|---|---|---|---|---|
| momentum_continuation | +0.2674 | +0.2492 | no | 5.50% | 4.43% | yes |
| mean_reversion_reclaim | +0.31243 | +0.31219 | no | 4.66% | 3.75% | yes |

f cuts give-back on both but loses expectancy on both, so the rule ships
**b: `daily_profit_lock: [[1.0, 0.0]]`** (stop to breakeven once a
completed 5-minute bar closes at +1R).

**Deploy - mid-session, by owner override (2026-10-09).** The owner
overrode the 16:15 ET deploy window and deployed S16 during the session.
Sequence: worker stopped cleanly at 11:27 ET; SPCX (1 @ 163.41, +$9.16) and
PLTR (2 @ 205.26, +$4.89) market-closed at 11:28 ET under the owner's
authorized cutover exception (bracket legs cancelled first); `orders.py
sync` journaled both (rows 39-40); profit lock merged; worker restarted
once via launchd. The PLTR BUY (row 37) is excluded from the
mean_reversion_reclaim probation count (`setup_probation.exclude_trade_ids`).


---

## 14. Study: trade funnel (S21, 2026-10-13) — study only, nothing ships without a CEO decision

Commissioned as S21. `study_trade_funnel.py` (read-only: journal opened
read-only, bars fetched from Alpaca market data into the git-ignored
`data/s21/`, no config, strategy, position or journal writes). Full output:
`reports/s21_trade_funnel.md`.

**Window:** the 30 NYSE sessions 2026-08-31 .. 2026-10-12. The worker
journaled on 26 of them; it was **silent on 09-30, 10-01, 10-02 and 10-12**
(10-12: launchd started it ~12 times; DNS failures, then Alpaca
`unauthorized` on `get_account` after the 10-09 account switch — no cycle
completed). SPY regime read chop on 14 of 30 sessions.

A **signal** = (ET date, ticker, setup, signal bar), placed at the furthest
gate it reached, in the live order of `streamlit_app._worker_loop`. Worker
setups only — CEO order-sheet rows are excluded. A gatekeeper "approval"
below conviction 70 is counted as the rejection the worker treats it as.

### Funnel

| Stage (live order) | Removed | Alive after |
|---|---|---|
| Detected (a trigger fired) | — | 5,314 |
| Rules filters (detector) | 5,033 | 281 |
| Circuit breaker | 0 | 281 |
| Regime: `spy_bearish` (chop, setup not exempt) | 42 | 239 |
| Entry window: `outside_hours` | 46 | 193 |
| `max_positions` / `check_signal` | 1 | 192 |
| **Gatekeeper** (192 asked) | **175** | **17** (9% pass) |
| Probation: `probation_position_open` | 10 | 7 |
| Size: `size_zero` / `price_too_high_for_account` | 3 | 4 |
| Duplicate / bracket failure | 0 | 4 |
| **Filled** | | **4** (SWKS, SPCX, ON, PLTR) |

Rules-filter detail: `adx_low` 4,220 (all trend_continuation, 5-minute
crosses), `volume_low` 474, `below_ema9` 148, `rsi_low` 67,
`gap_below_reclaim_mid` 47, `change_too_small` 43, others 34. Gatekeeper
detail (bucketed by what the reason cites): RSI 92, ADX 30, conviction <70
23, resistance 16, volume 9, unstated/other 5. Chop-reclaim exemptions
(Rule #5) used: 47.

**Every gatekeeper pass is accounted for:** 17 = 10 probation + 3 size + 4
filled. The 3 size rejections (ARM, NET, SPOT) were at the old account's
~$1,975 equity — the cap rose to $5,000 on 09-21 but the account held
~$2,000. **All three are affordable at $5,000**: the size stage disappears
on the new account.

### Rejections replayed 10 sessions

Geometry from the journal where it exists (gatekeeper, probation, size rows);
otherwise **rebuilt with the detector's own formula on the same bar** (daily:
entry ~ the decision session's open, stop/target per setup; trend: 5-minute
IEX bars, entry at the next bar's open, stop 2.0 x ATR14, 3R). Daily exits
use today's live rule (bracket + 1R -> breakeven lock); trend uses a static
bracket. The decision session is bar 1, and a bar spanning both levels is a
stop. Signals younger than 10 sessions at 10-12 are marked to the last
close ("unres.").

| Gate | Reason | Replayed | Unres. | Win % | Avg R |
|---|---|---|---|---|---|
| rules | adx_low (trend) | 4,195 | 6 | 25% | -0.04 |
| rules | volume_low | 440 | 24 | 25% | +0.14 |
| rules | below_ema9 (reclaim) | 138 | 19 | 20% | **-0.41** |
| rules | rsi_low (trend) | 67 | 1 | 21% | -0.18 |
| rules | gap_below_reclaim_mid | 36 | 1 | 19% | +0.60 |
| rules | change_too_small (momentum) | 40 | 6 | 25% | -0.27 |
| rules | gap_below_signal_mid | 10 | 0 | 10% | -0.40 |
| regime | spy_bearish | 41 | 0 | 22% | **-0.39** |
| window | outside_hours | 46 | 5 | 39% | +0.27 |
| gatekeeper | RSI | 92 | 20 | 41% | +0.18 |
| gatekeeper | ADX | 30 | 3 | 40% | +0.16 |
| gatekeeper | conviction < 70 | 23 | 11 | 48% | +0.40 |
| gatekeeper | resistance (trend) | 16 | 0 | 25% | 0.00 |
| gatekeeper | volume | 9 | 1 | 56% | +1.22 |
| post-gate | **probation_position_open** | **10** | 1 | **50%** | **+0.55** |
| post-gate | size (all 3) | 3 | 1 | 67% | -0.14 |
| — | the 4 actual fills, same replay | 4 | 1 | 25% | +0.02 |

Not replayable: post-earnings `not_first_close_above` / `no_earnings_event`
(14; this needs the earnings calendar) and about 85 others whose bars did not
line up: a daily signal bar that is not the session before the decision, or
5-minute bars missing for the bar.

Gatekeeper, passes vs rejections, **resolved signals only**: passed 14 —
43% win, +0.26R; rejected 140 — 32% win, +0.10R. On reclaim alone: passed
12 at +0.36R, rejected 96 at **+0.18R**. The gatekeeper picks the better
trades, but the reclaim signals it throws away are still positive on this
replay.

### Trades per week at $5,000 (capacity simulation, full uptime)

Each simulated session is a resampled real session's stream of
gatekeeper-passed signals, with their replayed outcome and holding time,
pushed through probation (one open position per probation setup, counting
every position — today's rule), `max_positions` 4, one position per ticker,
and `risk.position_size` at $5,000 / 1% / 25% cap / 0.15 lot tolerance.
Holding time runs to the end of the data (no time stop, as live). 2,000
runs.

**Calibration:** the real stream in date order, silent sessions empty, at
~$1,975 gives **3 fills; the desk made 4.** The difference is SWKS and SPCX,
which were open at the same time: both pre-date the v5 prompt and have no
prompt version, so neither held the reclaim slot (`risk.probation_open_tickers`).
The simulation applies the stricter rule that holds from now on.

| Scenario | Trades / week | 80% range | Win % | Avg R / trade |
|---|---|---|---|---|
| (a) current rules at $5,000 | **0.78** | 0.50–1.00 | 39% | +0.18 |
| (b) watchlist x2 | 1.14 | 0.83–1.50 | 30% | -0.04 |
| (c) probation `max_concurrent` 2 | **1.21** | 0.83–1.50 | 48% | +0.43 |
| (d) entry window 09:30–15:30 | 0.88 | 0.50–1.17 | 40% | +0.21 |
| (e) oversold_bounce lane, chop only | 0.98 | 0.67–1.33 | 40% | +0.33 |
| (c) + (d) + (e) | 1.53 | 1.17–2.00 | 47% | +0.48 |

- **(b)** draws two real sessions per simulated session. Extra supply mostly
  meets a full probation slot, so the added fills are the overflow:
  momentum and late reclaim entries. More trades, lower R.
- **(d)** adds the 42 replayable `outside_hours` signals from 11:30–14:00 and
  15:00–15:30, each passing the gatekeeper at its setup's observed rate
  (reclaim 11%, momentum 11%, trend 0% — the gatekeeper passed no
  trend_continuation signal in the window).
- **(e)** `backtest.detect_oversold_bounce` on the 188 tickers the worker
  scanned, chop sessions only: **15 fires, 53% win, +1.08R before any
  gatekeeper**, then reclaim's 11% pass rate and its own probation slot.
  It needs a new `strategies/` module and a "require chop" gate that no
  config key expresses today (item 11).

**Read the trade counts, not the R columns.** Every R figure in this table
rests on 17 gatekeeper passes. The ±0.2R gaps between scenarios are inside
the noise.

### Also found (not asked)

1. **`pullback_in_uptrend` cannot fire live.** It needs 62 daily bars; the
   worker fetches `lookback_days=60` calendar days (~41 bars,
   `streamlit_app.py:735`). Zero journal rows ever. It is a probation setup
   that can never earn its 20 trades.
2. **Uptime cost more than any scenario gains.** 4 of 30 sessions were
   silent, and 10-12 failed on auth. At 0.78 trades/week that is about 0.6
   of a trade lost. Fixing the 10-12 auth failure is an ops task, not a
   ruling.
3. The 10 probation blocks include DDOG, LRCX and NET on 09-23. Those were
   held out by v4 positions under the counting rule that was fixed later.

### Recommendation (PM, for the CEO — nothing ships without a decision)

1. **Ops first, no ruling needed:** get the worker running on the new
   account's keys, then confirm a full session journals.
2. **Ratify (c): probation `max_concurrent` 1 -> 2.** It is the largest
   single lever: 0.78 -> 1.21 trades/week. It frees the constraint that
   binds the desk's best-replaying population (10 blocks, +0.55R). Cost:
   while reclaim is 0/20 on probation, two unproven positions can be open
   at once, and worst-case exposure on the setup doubles to 2% of equity
   ($100). Daily loss breaker unchanged. Review again at 10 v5 trades.
3. **Commission, don't ship, (e):** build oversold_bounce as a journal-only
   shadow lane (detect + journal, no orders) to measure its gatekeeper pass
   rate and live fires in chop. 15 fires is not evidence for a live lane.
4. **Decline (b) for now.** Supply isn't the constraint while probation is
   one slot, and the extra fills replay worse.
5. **(d) is optional:** +0.10 trades/week. The `outside_hours` population
   replays +0.27R on 46, but the window is hard-coded
   (`streamlit_app.py:908`), so this is a code change.
6. **Order two follow-up studies:** (i) the gatekeeper's conviction
   threshold, since the 23 signals approved below 70 replay +0.40R (half
   unresolved); (ii) the `pullback_in_uptrend` bar-count fix, as an S-item.

**Caveats.** Rebuilt daily entries use the decision session's open; the
live entry is the price at evaluation, usually 09:3x. Trend exits ignore the
intraday ATR trail. The universe for (e) is the tickers the worker touched
in the window, not each day's exact scan. The profit lock went live on
10-09, so the replay applies today's rules to the past six weeks. These
figures estimate the current rules forward, not what the desk would have
earned.

### Decision (CEO, 2026-10-15)

**Ratified: recommendation 2 — `setup_probation.max_concurrent` 1 -> 2.**
Shipped as config only (`bot_config.json`); `risk.check_setup_probation`
already reads the value, so no code changed. Effective from the 2026-10-15
09:25 ET start. Each probation setup may now hold two open positions:
worst-case risk on one setup is 2% of equity ($100; $125 if both entries
carried the 1.25x conviction sizing). Daily loss breaker (4%) unchanged.
Review again at 10 v5 reclaim trades, per the recommendation. The other
recommendations of item 14 stand as written: (b) declined, (d) optional,
(e) a journal-only shadow lane to be commissioned, not shipped.

---

## 15. DECISION — gatekeeper resistance rule: keep (CEO, 2026-10-14)

**Ruling: keep the rule. No change.** Read-only evidence below (ops order
of 2026-10-14, item 4); nothing in config, prompts or code moves.

**Population:** every gatekeeper rejection since 2026-09-01 whose *stated*
`rejection_reason` cites distance to resistance or the 20-bar high: **33**
(30 trend_continuation, 3 mean_reversion_reclaim). Another 60 rejections
mention resistance only in the free-text reasoning and are excluded, as are
6 whose only match was the RSI "overextension ceiling".

**Outcome source:** the outcome tracker (`outcomes.py`, daily bars, 10
sessions) answers the 3 reclaim rows. It reports trend_continuation as
`no_geometry` by design — 5-minute stops on daily bars are a category error —
so those 30 were replayed on 5-minute IEX bars with the S21 method (entry and
geometry as journaled, static bracket, 10 sessions, a bar spanning both
levels counts as a stop).

| Date | Ticker | Setup | Outcome | R |
|---|---|---|---|---|
| 09-02 | FCX | trend | stop | -1.00 |
| 09-02 | INTU | trend | stop | -1.00 |
| 09-02 | WBD | trend | stop | -1.00 |
| 09-08 | ORCL | reclaim | stop (tracker) | -1.00 |
| 09-22 | TSLA | reclaim | stop (tracker) | -1.00 |
| 09-22 | AMD | trend | stop | -1.00 |
| 09-22 | NVDA | trend | stop | -1.00 |
| 09-22 | META | trend | stop | -1.00 |
| 09-22 | BKNG | trend | stop | -1.00 |
| 09-22 | MU | trend | **target** | +3.00 |
| 09-22 | MRVL | trend | stop | -1.00 |
| 09-22 | AAL | trend | stop | -1.00 |
| 09-23 | AMZN | trend | stop | -1.00 |
| 09-24 | MMM | reclaim | stop (tracker) | -1.00 |
| 09-25 | ABT | trend | stop | -1.00 |
| 10-05 | ZS | trend | **target** | +3.00 |
| 10-05 | NOW | trend | stop | -1.00 |
| 10-05 | ISRG | trend | **target** | +3.00 |
| 10-05 | JPM | trend | stop | -1.00 |
| 10-06 | BRK.B | trend | stop | -1.00 |
| 10-08 | INTU | trend | stop | -1.00 |
| 10-08 | MCD | trend | **target** | +3.00 |
| 10-08 | SHOP | trend | stop | -1.00 |
| 10-08 | COST | trend | stop | -1.00 |
| 10-08 | LMT | trend | stop | -1.20 |
| 10-08 | VALE | trend | stop | -1.03 |
| 10-09 | ORCL | trend | stop | -1.00 |
| 10-09 | SHOP | trend | **target** | +3.00 |
| 10-09 | MA | trend | **target** | +3.00 |
| 10-13 | NOW | trend | stop | -1.00 |
| 10-13 | NU | trend | stop | -1.00 |
| 10-13 | FANG | trend | unresolved | (+1.31 at last close) |
| 10-13 | TTWO | trend | unresolved | (-0.38 at last close) |

| | Resolved | Targets | Stops | Win % | Avg R |
|---|---|---|---|---|---|
| trend_continuation (5-min replay) | 28 | 6 | 22 | 21% | -0.15 |
| mean_reversion_reclaim (tracker) | 3 | 0 | 3 | 0% | -1.00 |
| **All** | **31** | **6** | **25** | **19%** | **-0.23** |

The signals the rule removes lose money as a group: at a 3R target the
break-even win rate is 25%, and these resolve at 19%. The rule is earning its
keep. Revisit only if a later sample of 30+ resolved replays comes in above
25%.
---

## 16. DECISION — daily percent lock replaces the R-step profit lock (S24, CEO + owner, 2026-10-14)

**CEO + owner decision, 2026-10-14.** The daily profit lock is replaced by a
percent lock (S24), effective for every open and new DAILY position,
including TXN. Intraday positions are unchanged (ATR trail + breakeven
floor). Owner override of the deploy window was given, as on 10-09; in the
event the merge landed at ~21:40 ET, after the 16:15 shutdown, so no live
session ran on mixed code.

### The rule (bot_config.json `daily_trailing: "pct_lock"`, `daily_pct_lock`)

Decided on the last COMPLETED 5-minute close, every worker cycle, in
percent of the BROKER's average fill:

| Step | When | Stop becomes |
|---|---|---|
| step1 | close >= entry x 1.0075 | original stop + 0.5 x (entry - original stop) |
| step2 | close >= entry x 1.0125 | entry x 1.001 |
| trail | highest close since entry >= entry x 1.02 | highest close x 0.99 |

The highest candidate wins. A stop is never lowered, and never placed
within 0.1% of the last price (it is capped 0.1% below). Prices round UP
to the cent. Each raise is journaled as a TIGHTEN_STOP row (old -> new,
step), positions are written immediately, and a Discord alert goes out via
alerts.post_async. A restart is a no-op (the raised level is persisted).

### TXN - entry is the broker fill

The decision text gave TXN's entry as 282.815. That is the SIGNAL price
(decision 12409 context). Alpaca's entry order filled at **280.25**
(filled 09:32:53 ET; the position's avg_entry_price is 280.25), and journal
row 41 already holds 280.25. The rule as written uses the broker fill, so
TXN's live triggers are lower than the decision's worked example:

| | At 282.815 (example) | At 280.25 (TXN live) |
|---|---|---|
| step1 trigger -> stop | 284.94 -> 280.41 | 282.36 -> 279.13 |
| step2 trigger -> stop | 286.36 -> 283.10 | 283.76 -> 280.54 |
| trail from | 288.47 | 285.86 |

Two arithmetic notes: the example triggers 284.93 and 286.35 are a
fraction of a cent below entry x 1.0075 / x 1.0125, so the first cents
that trigger are 284.94 and 286.36; and entry x 1.001 = 283.0978 rounds to
283.10 (the example's 283.09 is a round-down). Row 41 was NOT changed to
282.815 - that would replace a broker fill with a reference price.


### Superseded the same evening — owner ladder (S24b, 2026-10-14)

Before the percent lock ever ran live, the owner replaced step1/step2/trail
with a ladder (`daily_trailing: "ladder"`, `daily_lock`):

- once a completed 5-minute close >= entry x 1.005 -> stop = entry (breakeven);
- then stop = max(stop, highest close since entry x 0.995).

Never lowered; at least 0.1% below the last price; rounded up to the cent;
original stop and 3R target unchanged; each raise journaled (TIGHTEN_STOP)
and alerted via alerts.post_async. Entry is the broker's average fill.

The order text also said the stop rises in 0.25%-of-entry steps
(`step_pct`), but its own test numbers (TXN at 282.815: 284.23 -> 282.82,
284.93 -> 283.51) only hold for a continuous trail - stepping would give
282.82 at 284.93. The owner chose the **continuous** trail; `step_pct` is
not in the config. At TXN's real 280.25 fill, breakeven arms at a 281.66
close.

Reporting: the Discord weekly line now also shows, for the week's closed
daily trades, what "cushion 0.25%" and the "old +1R rule" would have made,
replayed on 5-minute bars (lock_replay.py).
---

## 17. DECISION — R/ATR ladder for daily positions, owner ladder as shadow (S25, CEO, 2026-10-14)

**CEO decision 2026-10-14, after the stop-loss research.** The owner's 0.5%
ladder (S24b) is replaced for LIVE daily positions by an R/ATR ladder; the
owner ladder keeps running as a SHADOW. Intraday positions are unchanged.

### Live rule (bot_config.json `daily_trailing: "r_atr"`, `daily_r_atr`, `daily_exits`)

R = entry (the broker's average fill) - original stop.

| When | Stop becomes |
|---|---|
| new entry | structural low - 0.25 x ATR14, distance clamped to 1.5-2.5 ATR (target kept at the same R multiple) |
| +1.0R | entry - 0.25R |
| +1.5R | entry |
| +2.0R | entry + 1R |
| beyond | max(stop, highest completed daily close - 2.5 x ATR14) |

Decided on completed 5-minute closes of bars starting at/after 10:00 ET and
only after 10:00 ET; never lowered; at least 0.1% below the last price;
stop-market legs. Existing positions keep their stop (TXN: R = 280.25 -
278.00 = 2.25; +1R at 282.50 -> 279.69, +1.5R at 283.63 -> 280.25, +2R at
284.75 -> 282.50).

Time stop: exit at the day-5 close (15:55 ET; entry day = day 1) if the best
move (highest 5-minute high) was under +0.5R; exit at the day-10 close
regardless. Earnings: if the company reports after today's close or before
the next session, exit at 15:50 ET; an unreachable calendar does not force
an exit (journaled as an ops event).

### Shadow (no orders)

The owner ladder - breakeven at +0.5%, then 0.5% under the highest close in
0.25%-of-entry steps - is replayed on 5-minute bars for every closed daily
trade. The Discord weekly line shows live vs shadow: P&L, win %, and capture
ratio (realized / best move during the hold).

### Not done here

- **S25 study (the ranked grid) is blocked**: the research report
  `reports/Swing trading stop loss methods.md` is not in this repository,
  and the grid is defined there.
- The order said to keep "the s24 dashboard sell button and trade-log
  changes"; no such dashboard button or trade-log change exists in this
  repository (S24 here was the percent lock), so there was nothing to keep.

Defaults chosen where the order was silent: ATR14 = daily ATR as of the last
completed day; "highest daily close" = completed daily closes since entry;
time-stop exits at 15:55 ET, earnings exits at 15:50 ET.


---

## 18. INCIDENT — positions recorded at the signal price, not the fill (S23, 2026-10-15)

**What happened.** Every worker entry since the Alpaca migration kept the
signal's reference price as the POSITION's entry. The fill poll compared
`str(order.status).lower()` to `"filled"`; alpaca-py's `str(OrderStatus.FILLED)`
is `"OrderStatus.FILLED"`, so the check could never pass, the poll always
timed out, and `entry_price` stayed at the reference. The journal looked right
because `orders.sync` later corrected the BUY row from the broker; the position
record (`positions.json`), the "BOUGHT" log line and the Discord alert never
were. The same comparison sat in `confirm_fill` (hard-exit / manual closes).
The CEO order-sheet and intern paths normalised the status and were correct.

| | Signal reference | Broker fill | Position used |
|---|---|---|---|
| TXN 10-14 | 282.815 | **280.25** | 282.815 |
| REGN 10-15 | 806.24 | **802.61** | 806.24 (then cleared by the position-gone bug; re-adopted at 802.61) |

**Consequence (TXN).** R and every lock trigger were computed from the wrong
entry: R = 4.815 instead of 2.25, so +1R sat at 287.63 instead of **282.50**.
TXN closed at 282.67 at 10:05 and as high as 283.50 on 10-14 - the ladder's +1R
step never fired, the stop stayed at the structural 278.00, TXN gapped down on
10-15 and the stop filled at 278.78 / 277.79: **-$6.87 (-0.61R)**.

**Replay under the fixed code and the live R/ATR ladder** (5-minute IEX bars,
entry 280.25, original stop 278.00, steps +1R -> -0.25R, +1.5R -> 0, +2R -> +1R,
qualifying closes from 10:00 ET):

| Time (ET) | Event | Stop |
|---|---|---|
| 10-14 09:32 | fill 4 @ 280.25 | 278.00 |
| 10-14 10:05 | close 282.67 >= +1R (282.50) | **279.69** (+1R step) |
| 10-14 | highest close 283.50 = +1.44R (BE step at +1.5R = 283.625 not reached) | 279.69 |
| 10-14 13:55 | bar opens 279.62, through the stop | exit @ 279.62 |

Result: **-$2.52 (-0.28R) instead of -$6.87 (-0.61R)** - $4.35 kept on one
4-share trade. (Replay on the IEX feed; broker fills come from its own feed.)

**Fix (S23, merged 2026-10-15).**
1. The entry poll compares the normalised status; on a fill the position takes
   the broker's `filled_avg_price` and `filled_qty` (partial entries use the
   partial's avg and qty) and keeps the reference as `signal_price`. "BOUGHT"
   and the Discord alert show the fill, or say "fill pending" - never a
   reference price as a fill. `confirm_fill` fixed the same way.
2. Every cycle, BEFORE any lock/trail math, each open position's entry and qty
   are reconciled to the broker's `avg_entry_price` / qty; a correction is
   persisted at once and journaled as an `entry_reconciled` ops event. R is
   always `entry_price - original_stop`, so triggers recompute. A smaller broker
   qty while an exit leg has filled anything is a partial EXIT and never shrinks
   the position (caught by the 10-15 replay test).
3. Ledger: TXN SELL row 43 corrected to 278.5325 / -$6.87 from the broker's fills
   (CEO approval); TXN BUY row 41 (280.25) and REGN BUY row 42 (802.61) were
   already the real fills.

Related the same day: the "position gone" clear of REGN (entry not yet filled)
and the TXN exit journaled on a partial fill - both fixed in `877d9e5`.

### Historical impact (S27, 2026-10-15)

Every closed bot trade since the Alpaca migration, R recomputed from the
real fill and replayed under the exit rule that was live at the time. The
bug was live throughout - the logged "BOUGHT" price equals the reference on
every trade the Mac's log covers (SWKS, SPCX, ON, PLTR, TXN, REGN).

| Trade | Fill vs reference | Gap / R | Rule live at the time | Effect |
|---|---|---|---|---|
| UBER 08-10, ORCL 08-13, NOK 08-13 | -0.075 / -0.07 / +0.025 | 1-5% | pre-08-13: ATR trail with no +1R gate | none - entry not in the stop math |
| BMNR 08-20 -> 09-01 | +0.195 (fill above) | 6% | +1R gate, then ATR trail | trail opened 08-24 instead of 08-25; the binding trail level came from the 08-27 peak either way - **$0** |
| CRCL 08-21 -> 09-09 | +0.11 | 1% | +1R gate / breakeven floor | +1R hit 09-03 under both - **$0** |
| SLB 08-28 -> 09-08 | +0.115 | 4% | +1R gate / breakeven floor | +1R hit 08-31 under both - **$0** |
| SWKS 09-11 -> 10-09 | +1.77 | 12% | breakeven floor, then S6 static | +1R never reached (high 92.63 vs 101.24 / 104.78) - **$0** |
| SPCX 09-17 -> 10-09 | +0.15 | 2% | S6 static bracket; closed by hand | **$0** |
| ON 10-05 -> 10-07 | -0.73 (fill below) | 41% | S6 static bracket - daily positions did not trail | **$0** |
| PLTR 10-09 | +1.58 | 22% | S6 static; closed by hand at the cutover | **$0** |
| **TXN 10-14 -> 10-15** | **-2.565** | **114%** | **S16 lock: breakeven at +1R** | **lock should have fired, did not** |

**TXN under the rule live on 10-14 (S16).** With the real fill the 10:05
close (282.67 >= +1R 282.50) locks the stop at 280.25; TXN trades back
through it at 12:30 -> exit 280.25, **$0.00**. As run: +1R sat at 287.63,
never reached; stop 278.00; filled 278.78 / 277.79 on the 10-15 gap ->
**-$6.87**. (Under today's R/ATR ladder the same trade would have exited at
279.62, -$2.52 - the replay above.)

**Total historical cost of the entry bug: $6.87, all on TXN.** The other 12
closed trades either ran under rules where the entry did not enter the stop
math, or reached (or missed) +1R on the same day under both entries.
---

## 19. Study: S29 exit variants - +1R step and reward:risk after the target cap (order C, 2026-10-17)

**CEO 2026-10-17, order C. Study only - no config change.** The live daily rule (S25 R/ATR ladder, S28 target cap at min(3R, 3.0 x ATR14), time stops) replayed on 5-minute bars, against: the +1R step at breakeven instead of entry - 0.25R; and, after the cap, the initial stop clamped at 2.0 ATR, or skipping trades whose capped reward:risk is under 1.5. Reproduce with `python study_exit_grid.py`.

**S29 (order C) - 5-minute replay of the live daily rule, 342 backtest signals in the bar window.** Study only.

| Setup | Variant | Trades | Skipped | Win % | Expectancy (R) | PF | MaxDD (R) | Capture | Give-back (+1R then loss) |
|---|---|---|---|---|---|---|---|---|---|
| all | +1R->-0.25R / current | 287 | 0 | 46.3% | +0.315 | 1.84 | 13.99 | 30% | 8.7% |
| all | +1R->-0.25R / stop<=2.0ATR | 287 | 0 | 45.3% | +0.314 | 1.82 | 13.53 | 30% | 9.1% |
| all | +1R->-0.25R / skip R:R<1.5 | 244 | 57 | 43.9% | +0.305 | 1.77 | 15.62 | 29% | 9.0% |
| all | +1R->BE / current | 287 | 0 | 46.3% | +0.321 | 1.91 | 14.16 | 31% | 2.1% |
| all | +1R->BE / stop<=2.0ATR | 287 | 0 | 46.0% | +0.322 | 1.89 | 14.33 | 31% | 2.1% |
| all | +1R->BE / skip R:R<1.5 | 244 | 57 | 44.3% | +0.310 | 1.82 | 14.7 | 29% | 2.5% |
| momentum_continuation | +1R->-0.25R / current | 67 |  | 34.3% | +0.086 | 1.17 | 15.43 | 9% | 14.9% |
| momentum_continuation | +1R->-0.25R / stop<=2.0ATR | 67 |  | 32.8% | +0.066 | 1.13 | 15.48 | 6% | 16.4% |
| momentum_continuation | +1R->-0.25R / skip R:R<1.5 | 53 |  | 35.8% | +0.180 | 1.36 | 12.25 | 16% | 17.0% |
| momentum_continuation | +1R->BE / current | 67 |  | 38.8% | +0.114 | 1.24 | 15.19 | 11% | 4.5% |
| momentum_continuation | +1R->BE / stop<=2.0ATR | 67 |  | 38.8% | +0.098 | 1.2 | 15.23 | 10% | 4.5% |
| momentum_continuation | +1R->BE / skip R:R<1.5 | 53 |  | 41.5% | +0.212 | 1.45 | 12.0 | 19% | 5.7% |
| mean_reversion_reclaim | +1R->-0.25R / current | 220 |  | 50.0% | +0.385 | 2.15 | 9.29 | 36% | 6.8% |
| mean_reversion_reclaim | +1R->-0.25R / stop<=2.0ATR | 220 |  | 49.1% | +0.390 | 2.14 | 8.99 | 36% | 6.8% |
| mean_reversion_reclaim | +1R->-0.25R / skip R:R<1.5 | 191 |  | 46.1% | +0.340 | 1.92 | 11.55 | 32% | 6.8% |
| mean_reversion_reclaim | +1R->BE / current | 220 |  | 48.6% | +0.384 | 2.21 | 9.29 | 36% | 1.4% |
| mean_reversion_reclaim | +1R->BE / stop<=2.0ATR | 220 |  | 48.2% | +0.390 | 2.2 | 9.1 | 36% | 1.4% |
| mean_reversion_reclaim | +1R->BE / skip R:R<1.5 | 191 |  | 45.0% | +0.337 | 1.96 | 11.38 | 32% | 1.6% |

### Reading

- **+1R step: breakeven vs entry - 0.25R.** Breakeven is at least as good on
  every measure: expectancy +0.321R vs +0.315R (all), PF 1.91 vs 1.84, and it
  cuts the give-back (trades that reached +1R and still lost) from 8.7% to
  2.1%. Momentum gains most (+0.114R vs +0.086R, give-back 4.5% vs 14.9%);
  on reclaim the expectancy is unchanged (+0.384R vs +0.385R) while the
  give-back falls 6.8% -> 1.4%. Capture is the same (~30-31%).
- **Stop clamp at most 2.0 ATR** changes almost nothing (+0.314R vs +0.315R):
  the clamp rarely binds, because few structural stops sit beyond 2 ATR.
- **Skip capped R:R < 1.5** removes 57 of 301 candidate trades. It helps
  momentum (+0.180R / +0.212R vs +0.086R / +0.114R on 53 vs 67 trades) but
  hurts reclaim (+0.340R vs +0.385R), so overall expectancy falls
  (+0.305R / +0.310R vs +0.315R / +0.321R).

**Limits.** One ~6-month window of 5-minute bars (no held-out year - the
cache does not reach back a year); earnings exits are not modelled (no
historical calendar); backtest signals, not trades the gatekeeper approved.
The full S29 grid (trail 2.0/2.5/3.0 ATR, target 1.5R/2R/3R, half-at-1R,
time stops) is defined in the research report, which is still not in this
repository.


### Decision (CEO 2026-10-17): adopt +1R -> breakeven

**Adopted.** The daily R/ATR ladder's first step is now **+1R -> stop to
entry (breakeven)**, replacing entry - 0.25R: `daily_r_atr.steps` =
`[[1.0, 0.0], [1.5, 0.0], [2.0, 1.0]]` in both risk profiles (and the code
default). Basis: the S29 replay above - expectancy +0.321R vs +0.315R, PF
1.91 vs 1.84, give-back 2.1% vs 8.7%, capture unchanged. The +1.5R step now
asks for the same level and is a no-op; kept so the ladder's shape stays in
config. The stop-clamp and skip-R:R<1.5 variants are **not** adopted.
Existing positions are not moved: the ladder only acts when a position's
qualifying close reaches +1R - REGN (entry 802.61, stop 756.25, +1R 848.97,
highest close so far 808.37) keeps its stop until then.
---

## 20. Study: S33 ADX threshold band - trend_continuation, 5-minute replay 2025-10-14..2026-10-19

**S33 (CEO 2026-10-19), study only.** trend_continuation, the only setup with an ADX gate, replayed on 5-minute IEX bars 2025-10-14..2026-10-19 (53 weeks, 173 universe symbols) through the live filter chain and the CURRENT intraday exit rule, at four ADX thresholds. "adx_low only" = the signals the live threshold blocks, admitted by the lower one.

| Threshold | Population | Trades | Trades/week | Win % | Expectancy (R) | Total R | MaxDD (R) |
|---|---|---|---|---|---|---|---|
| current (30) | all admitted | 582 | 11.0 | 40.9% | +0.133 | +77.15 | 28.97 |
| -5 (25) | all admitted | 1342 | 25.4 | 38.1% | +0.088 | +118.12 | 73.5 |
| -5 (25) | adx_low only | 760 | 14.4 | 35.9% | +0.054 | +40.98 | 55.48 |
| -10 (20) | all admitted | 2696 | 51.0 | 37.6% | +0.066 | +176.97 | 47.76 |
| -10 (20) | adx_low only | 2114 | 40.0 | 36.7% | +0.047 | +99.82 | 45.97 |
| regime (SPY ADX) | all admitted | 1231 | 23.3 | 38.7% | +0.074 | +91.27 | 57.14 |
| regime (SPY ADX) | adx_low only | 649 | 12.3 | 36.8% | +0.022 | +14.12 | 36.12 |

**No threshold recommended.** None of the lower thresholds reaches expectancy >= +0.15R while raising the trade count; the current threshold stands.

**Method.** Filters in live order (cross on the last completed bar, ADX, RSI >= 55, confirmation, volume >= 20-bar EMA), SPY above its 20-day EMA, entries 09:30-11:30 / 14:00-15:00 ET at the close of the bar after the cross; stop 2.0 x ATR14(5m), target 3R capped at fill + 3 x daily ATR14 (S28), initial stop until +1R then a 2.5 x ATR14(5m) trail floored at entry; no end-of-day exit. Regime rule: SPY daily ADX14 >= 25 -> 20, 20-25 -> 25, < 20 -> 30. One position per symbol; the max-positions cap is not modelled. Reproduce with `python study_adx_band.py --s33 --fetch` (fetch after the close).

**Limits.** Today's universe (survivorship); IEX volume, as live; the gatekeeper is not modelled.

_Measured on SIGNALS, not on trades the desk took. Every signal counted here would still have had to clear the AI gatekeeper, whole-share sizing on a $2,000 cap, the max-positions cap and the daily-loss breaker, so these counts are an UPPER BOUND on what would have been realised._


### Reading

- **Lowering the threshold adds trades but dilutes the edge.** The signals
  the live ADX 30 gate blocks are worse than the ones it admits: +0.054R at
  ADX 25-30, +0.047R at 20-30, +0.022R under the regime rule, against
  +0.133R for the ADX >= 30 population. Total R rises with volume (+77R ->
  +118R / +177R), but so does drawdown (29R -> 74R / 48R), and every lower
  threshold falls below the +0.15R bar.
- **The regime-dependent rule is no better than a flat -5** (+0.074R vs
  +0.088R; its extra trades +0.022R): a strongly trending SPY does not make
  weak single-name ADX signals pay.
- **The current threshold itself is under the bar** (+0.133R) on this
  replay. Not a question this order asked; noted for the trend_continuation
  review, not acted on.

**No change proposed** - the live threshold stays at 30. Study only.
---

## 21. Study: S34 target grid - 1.5R/2R/2.5R/3R, with and without half at +1R (2026-10-20)

**S34 (CEO 2026-10-20) - target grid, study only.** 5-minute replay of the live daily rule, 2025-10-14..2026-10-19, 2305 signals on 171 universe symbols; current = 3R target (S28-capped), no partial.

| Setup | Target | Trades | Win % | Expectancy (R) | PF | MaxDD (R) | Capture | Exits at target |
|---|---|---|---|---|---|---|---|---|
| all | 1.5R | 2064 | 45.3% | +0.207 | 1.52 | 43.71 | 23% | 28.0% |
| all | 1.5R + half@1R | 2064 | 52.2% | +0.175 | 1.46 | 41.37 | 19% | 28.0% |
| all | 2R | 2059 | 43.2% | +0.215 | 1.53 | 44.08 | 22% | 20.4% |
| all | 2R + half@1R | 2059 | 52.0% | +0.179 | 1.47 | 41.49 | 18% | 20.4% |
| all | 2.5R | 2059 | 43.2% | +0.215 | 1.53 | 44.08 | 22% | 20.4% |
| all | 2.5R + half@1R | 2059 | 52.0% | +0.179 | 1.47 | 41.49 | 18% | 20.4% |
| all | 3R (current) | 2059 | 43.2% | +0.215 | 1.53 | 44.08 | 22% | 20.4% |
| all | 3R + half@1R | 2059 | 52.0% | +0.179 | 1.47 | 41.49 | 18% | 20.4% |
| momentum_continuation | 1.5R | 301 | 47.5% | +0.137 | 1.36 | 13.27 | 17% | 27.2% |
| momentum_continuation | 1.5R + half@1R | 301 | 53.5% | +0.148 | 1.42 | 12.02 | 18% | 27.2% |
| momentum_continuation | 2R | 301 | 45.8% | +0.136 | 1.34 | 14.93 | 16% | 21.9% |
| momentum_continuation | 2R + half@1R | 301 | 53.2% | +0.147 | 1.42 | 11.83 | 17% | 21.9% |
| momentum_continuation | 2.5R | 301 | 45.8% | +0.136 | 1.34 | 14.93 | 16% | 21.9% |
| momentum_continuation | 2.5R + half@1R | 301 | 53.2% | +0.147 | 1.42 | 11.83 | 17% | 21.9% |
| momentum_continuation | 3R (current) | 301 | 45.8% | +0.136 | 1.34 | 14.93 | 16% | 21.9% |
| momentum_continuation | 3R + half@1R | 301 | 53.2% | +0.147 | 1.42 | 11.83 | 17% | 21.9% |
| mean_reversion_reclaim | 1.5R | 1763 | 44.9% | +0.219 | 1.54 | 37.59 | 23% | 28.1% |
| mean_reversion_reclaim | 1.5R + half@1R | 1763 | 52.0% | +0.180 | 1.47 | 36.17 | 19% | 28.1% |
| mean_reversion_reclaim | 2R | 1758 | 42.7% | +0.229 | 1.57 | 39.12 | 22% | 20.2% |
| mean_reversion_reclaim | 2R + half@1R | 1758 | 51.8% | +0.184 | 1.47 | 36.94 | 18% | 20.2% |
| mean_reversion_reclaim | 2.5R | 1758 | 42.7% | +0.229 | 1.57 | 39.12 | 22% | 20.2% |
| mean_reversion_reclaim | 2.5R + half@1R | 1758 | 51.8% | +0.184 | 1.47 | 36.94 | 18% | 20.2% |
| mean_reversion_reclaim | 3R (current) | 1758 | 42.7% | +0.229 | 1.57 | 39.12 | 22% | 20.2% |
| mean_reversion_reclaim | 3R + half@1R | 1758 | 51.8% | +0.184 | 1.47 | 36.94 | 18% | 20.2% |
| pullback_in_uptrend | 1.5R | 0 | - | - | - | - | - | - |
| pullback_in_uptrend | 1.5R + half@1R | 0 | - | - | - | - | - | - |
| pullback_in_uptrend | 2R | 0 | - | - | - | - | - | - |
| pullback_in_uptrend | 2R + half@1R | 0 | - | - | - | - | - | - |
| pullback_in_uptrend | 2.5R | 0 | - | - | - | - | - | - |
| pullback_in_uptrend | 2.5R + half@1R | 0 | - | - | - | - | - | - |
| pullback_in_uptrend | 3R (current) | 0 | - | - | - | - | - | - |
| pullback_in_uptrend | 3R + half@1R | 0 | - | - | - | - | - | - |

**Adopt rule not met** - no variant has expectancy >= current AND higher capture over >= 200 trades. No change.

**Method.** Live ladder (+1R -> entry, +1.5R -> entry, +2R -> entry + 1R, then highest daily close - 2.5 x ATR14) on completed 5-minute closes from 10:00 ET; time stop day 5 if MFE < +0.5R, day 10 regardless; target = fill + min(kR, 3 x ATR14). Half at +1R: half exits at entry + 1R on the first touch. Fills: a bar opening through a level fills at its open; a bar spanning stop and target takes the stop. Capture = realized R / MFE R over trades that went green. Reproduce with `python study_exit_grid.py --s34`.

**Limits.** Today's universe (survivorship); IEX bars, daily resampled from them; earnings exits not modelled; post_earnings_continuation excluded (no historical calendar); signals, not gatekeeper-approved trades.

_Measured on SIGNALS, not on trades the desk took. Every signal counted here would still have had to clear the AI gatekeeper, whole-share sizing on a $2,000 cap, the max-positions cap and the daily-loss breaker, so these counts are an UPPER BOUND on what would have been realised._

### Reading

- **2R, 2.5R and 3R are the same trade.** The S28 cap sets target = fill + min(kR, 3 x ATR14), and the initial stop is clamped to at least 1.5 ATR, so R >= 1.5 ATR and any k >= 2 reaches the 3-ATR cap first. Under the cap the only target that differs from today's is 1.5R.
- **1.5R:** more exits at target (28.0% vs 20.4%) and capture 23% vs 22%, but expectancy +0.207R vs +0.215R (all) - fails the 'expectancy >= current' leg.
- **Half at +1R** raises the win rate (52% vs 43%) and trims drawdown (41.5R vs 44.1R) but costs expectancy (+0.179R vs +0.215R) and capture (18% vs 22%) overall; on mean_reversion_reclaim (1,758 trades) it costs 0.045R per trade.
- **Per setup, one cell passes the rule:** momentum_continuation 1.5R + half@1R, +0.148R vs +0.136R, capture 18% vs 16%, 301 trades. The edge is 0.012R; the config has no per-setup target or partial-exit setting, so adopting it would be new exit code, not a config change. **Not adopted - CEO decision.**
- pullback_in_uptrend shows 0 trades: its detector needs 62 daily bars and the backtest passes a 60-bar window (backtest.DETECT_WINDOW), so it never fires in any backtest. Pre-existing; not changed here.

**Decision under the adopt rule (all setups, >= 200 trades): no variant qualifies. No config change.**
---

## 22. Study: S35 pullback_in_uptrend - bar window fixed, first replay (2026-10-20)

**S35 (CEO 2026-10-20) - pullback_in_uptrend, first replay.** 5-minute replay of the live daily exit rule, 156 signals on 169 universe symbols, entries 2025-11-11..2026-10-19.

| Setup | Trades | Win % | Expectancy (R) | PF | MaxDD (R) | Capture | Exits at target |
|---|---|---|---|---|---|---|---|
| pullback_in_uptrend | 155 | 36.8% | -0.096 | 0.77 | 21.77 | -12% | 14.8% |

**Bar not met** (>= +0.1R over >= 30 trades): the setup stays on probation, max 1 concurrent.

**Method.** As S34 (agenda 21): daily bars resampled from the S33 5-minute IEX cache; live ladder, time stops and S28 target cap; SPY above its 20-day EMA. The detector needs 62 daily bars, so the first possible entry is ~62 sessions into the cache (2025-08-15), not 2025-10-14. Reproduce with `python study_exit_grid.py --s35`.

_Measured on SIGNALS, not on trades the desk took. Every signal counted here would still have had to clear the AI gatekeeper, whole-share sizing on a $2,000 cap, the max-positions cap and the daily-loss breaker, so these counts are an UPPER BOUND on what would have been realised._


### Reading and decision

- **The backtest window is fixed** (`backtest.DETECT_WINDOW` 60 -> 70; every
  setup's minimum is asserted in tests/test_s35_bar_window.py). This also
  shifts other setups' backtest numbers slightly (longer indicator history).
- **pullback_in_uptrend replays at -0.096R over 155 trades** (PF 0.77, win
  36.8%): below the +0.10R / 30-trade bar. **It stays in setup_probation
  with max 1 concurrent** (`setup_probation.max_concurrent_by_setup`), the
  other probation setups keep the shared 2 (agenda 14).
- **It has not traded live since 10-09 - and cannot.** No journal row
  (decision, rules pass or trade) for the setup since 10-09. The worker
  fetches daily bars with `lookback_days=60` (calendar days, ~41 sessions),
  below the detector's 62-bar minimum, so the live detector returns None
  before looking. Fixing it means a longer daily fetch for every daily
  setup (slightly different indicator values for reclaim and momentum too) -
  a trading-path change. **Not made; CEO decision.** Given the replay, the
  case for enabling it is weak.

### Decision (CEO 2026-10-21): pullback_in_uptrend RETIRED

**Retired from live trading.** Removed from `default_strategies` and from
`setup_probation` (and its `max_concurrent_by_setup` slot); `strategies.RETIRED`
keeps it out of the live detector list even if a config lists it again. The
detector stays in the registry for backtests and studies. Basis: the replay
above (-0.096R over 155 trades). The daily fetch is NOT changed: the
60-calendar-day window that starved it live stays as it is.

---

## 23. Study: S36 catalyst replay - catalyst names vs the rest (2026-10-21)

**S36 (CEO 2026-10-21) - catalyst names vs the rest, study only.** 5-minute replay of the live daily setups through the live daily exit rule at the current target (3R, S28-capped, no partial), 2324 signals on 161 universe symbols, entries 2025-10-14..2026-10-16. Catalyst = entry day had |gap| >= 3%, Finnhub company news in the 18 h before 09:30 ET, open price 5-1200, 20-day ADV >= $20M, top 15 |gap| a day.

| Split | Setup | Group | Trades | Win % | Expectancy (R) | PF | MaxDD (R) | Capture | Exits at target |
|---|---|---|---|---|---|---|---|---|---|
| gap + news (scanner rule) | all | catalyst | 100 | 30.0% | -0.169 | 0.67 | 20.46 | -20% | 17.0% |
| gap + news (scanner rule) | all | rest | 1963 | 44.0% | +0.151 | 1.38 | 44.39 | 17% | 20.8% |
| gap + news (scanner rule) | momentum_continuation | catalyst | 29 | 17.2% | -0.370 | 0.39 | 11.96 | -48% | 10.3% |
| gap + news (scanner rule) | momentum_continuation | rest | 266 | 49.2% | +0.201 | 1.54 | 8.15 | 22% | 23.7% |
| gap + news (scanner rule) | mean_reversion_reclaim | catalyst | 71 | 35.2% | -0.087 | 0.82 | 9.83 | -9% | 19.7% |
| gap + news (scanner rule) | mean_reversion_reclaim | rest | 1571 | 43.7% | +0.162 | 1.41 | 38.63 | 18% | 20.9% |
| gap + news (scanner rule) | pullback_in_uptrend | catalyst | 0 | - | - | - | - | - | - |
| gap + news (scanner rule) | pullback_in_uptrend | rest | 126 | 36.5% | -0.092 | 0.79 | 20.22 | -11% | 14.3% |
| gap only (no news test) | all | catalyst | 105 | 29.5% | -0.188 | 0.63 | 21.66 | -23% | 16.2% |
| gap only (no news test) | all | rest | 1958 | 44.1% | +0.153 | 1.39 | 44.39 | 17% | 20.9% |
| gap only (no news test) | momentum_continuation | catalyst | 29 | 17.2% | -0.370 | 0.39 | 11.96 | -48% | 10.3% |
| gap only (no news test) | momentum_continuation | rest | 266 | 49.2% | +0.201 | 1.54 | 8.15 | 22% | 23.7% |
| gap only (no news test) | mean_reversion_reclaim | catalyst | 76 | 34.2% | -0.118 | 0.75 | 11.56 | -14% | 18.4% |
| gap only (no news test) | mean_reversion_reclaim | rest | 1566 | 43.8% | +0.165 | 1.42 | 38.63 | 18% | 20.9% |
| gap only (no news test) | pullback_in_uptrend | catalyst | 0 | - | - | - | - | - | - |
| gap only (no news test) | pullback_in_uptrend | rest | 126 | 36.5% | -0.092 | 0.79 | 20.22 | -11% | 14.3% |

**Catalyst days.** Scanner rule: 1799 symbol-days (after the 15/day cap), 100 of them produced a trade. Gap only: 1888 symbol-days, 105 produced a trade. Gap/price/ADV symbol-days before the news test: 2310 on 151 symbols, 2163 with news in the window.

**Finnhub coverage.** 151 symbols queried (gap days +/- 1 day), 110269 articles, earliest 2025-10-26; 0 symbol(s) failed and count as no-news. Free tier: about one year of history, so the first entry days of the window can lack news.

**Method.** As S34 (agenda 21): daily bars resampled from the S33 5-minute IEX cache (RTH), live SPY gate, live ladder, time stops and S28 target cap, one position per symbol. Gap = entry day's first RTH 5-minute open vs the previous cached session's RTH close; ADV = mean close x volume of the 20 prior sessions (IEX volume, the same feed the live universe ADV uses). Reproduce with `python study_exit_grid.py --s36`.

_Measured on SIGNALS, not on trades the desk took. Every signal counted here would still have had to clear the AI gatekeeper, whole-share sizing on a $2,000 cap, the max-positions cap and the daily-loss breaker, so these counts are an UPPER BOUND on what would have been realised._


### Reading

- **Catalyst-day trades did worse than the rest.** Under the scanner rule,
  100 trades on catalyst days: win 30.0%, -0.169R, PF 0.67, against +0.151R
  (PF 1.38) for the other 1,963 trades. By setup: momentum_continuation
  -0.370R over 29 (win 17.2%), mean_reversion_reclaim -0.087R over 71;
  pullback_in_uptrend had no catalyst-day trades.
- **Few catalyst days produce a trade.** 1,799 catalyst symbol-days after the
  15/day cap; 100 (5.6%) produced an entry. Catalyst-day trades are ~5% of
  all trades, so the sample is small.
- **The news test barely changes the split.** 2,163 of the 2,310
  gap/price/ADV symbol-days (94%) had Finnhub news in the 18 h window. Gap
  only gives 105 catalyst trades at -0.188R; adding the news test removes 5
  of them. 57 of the 147 no-news gap days fall before Finnhub's earliest
  returned article (2025-10-26), so part of what the news test removes here
  is missing coverage, not missing news.
- **Limits.** Today's universe only: the scanner's purpose is to add names
  that are normally outside the universe, and those cannot be replayed from
  this cache, so "catalyst" here means a universe name on a gap-and-news
  day - a proxy. Gap is measured on IEX 5-minute RTH bars (first RTH open
  vs prior RTH close), not on the pre-market quote the scanner will see.
  Finnhub free tier: ~1 year of history (none before 2025-10-26), at most
  ~250 articles a call (70 capped windows refetched narrow; none left
  unresolved); 0 symbols failed. Signals, not gatekeeper-approved trades.

### Decision (CEO 2026-10-21): catalyst names are WATCH-ONLY

**Catalyst names are watch-only until a news-gap setup shows >= +0.15R over
>= 50 trades.** The scanner, the 09:20 Discord table and the
`source='catalyst'` journal rows are unchanged; the universe rows carry
`catalyst=true, watch_only=true`. A setup that fires on a watch-only name is
not entered: the worker logs "watch_only: catalyst" and journals a rules row
`watch_only_catalyst` with the signal's entry/stop/target, so the outcome
tracker grades what the entry would have done. A name already in the
universe without the catalyst stays tradable only if its gap is < 3% - and
every catalyst name gapped >= 3%, so there are no news-gap entries at all.
Basis: the table above (catalyst-day trades -0.169R over 100, momentum
-0.370R over 29).

There is no dedicated news-gap / morning-momentum setup in the code yet;
the replay above measured the EXISTING setups on catalyst days. The bar
applies to a dedicated setup once one is defined and replayed.
---

## 24. Study: S37 pyramiding on winners (2026-10-21)

**S37 (CEO 2026-10-21) - pyramiding on winners, study only.** 5-minute replay of the live daily rule, 2194 signals on 162 universe symbols, entries 2025-10-14..2026-10-20. Expectancy is R per ORIGINAL trade; $ at $5,000 equity, 1% = $50 per R.

| Setup | Variant | Trades | Adds | Win % | Expectancy (R) | Total R | PF | MaxDD (R) | Avg $ at risk / trade | Adds' own total R | Over 25% position cap |
|---|---|---|---|---|---|---|---|---|---|---|---|
| all | no add (current) | 1956 | 0 | 43.6% | +0.147 | +287.1 | 1.37 | 42.74 | $50.00 | +0.0 | 25.1% |
| all | add at +1R | 1956 | 747 | 39.9% | +0.155 | +303.0 | 1.3 | 57.43 | $69.10 | +16.0 | 47.4% |
| all | add at +1.5R | 1956 | 373 | 42.7% | +0.155 | +303.3 | 1.37 | 45.22 | $59.53 | +16.3 | 34.3% |
| momentum_continuation | no add (current) | 297 | 0 | 46.1% | +0.150 | +44.7 | 1.39 | 14.93 | $50.00 | +0.0 | 11.8% |
| momentum_continuation | add at +1R | 297 | 113 | 41.4% | +0.160 | +47.6 | 1.31 | 21.0 | $69.02 | +2.9 | 39.1% |
| momentum_continuation | add at +1.5R | 297 | 49 | 45.1% | +0.150 | +44.6 | 1.36 | 16.79 | $58.25 | -0.1 | 21.2% |
| mean_reversion_reclaim | no add (current) | 1659 | 0 | 43.2% | +0.146 | +242.4 | 1.36 | 38.63 | $50.00 | +0.0 | 27.5% |
| mean_reversion_reclaim | add at +1R | 1659 | 634 | 39.6% | +0.154 | +255.4 | 1.29 | 53.28 | $69.11 | +13.0 | 48.9% |
| mean_reversion_reclaim | add at +1.5R | 1659 | 324 | 42.3% | +0.156 | +258.7 | 1.37 | 41.11 | $59.76 | +16.3 | 36.6% |

**Method.** As S34 (agenda 21): daily bars resampled from the 5-minute IEX bars; live ladder, time stops, S28 target cap; SPY gate for momentum (the reclaim is exempt). The add is bought at the open of the bar after a completed close at or above entry + kR with the stop at breakeven or better; it is sized so the symbol's combined open risk stays within 1% of equity, with its own stop at the original entry, raised with the ladder. 'Avg $ at risk / trade' counts the first unit's $50 plus the add's $50 when one was made (the two are never at risk at the same time: the first unit is at breakeven when the add is bought). 'Over 25% position cap' is the share of trades whose combined position would exceed $1,250 - a live add would be trimmed or refused there. Reproduce with `python study_exit_grid.py --s37`.

**Limits.** Fractional shares (live sizing is whole shares); today's universe (survivorship); IEX bars; earnings exits not modelled; signals, not gatekeeper-approved trades.

_Measured on SIGNALS, not on trades the desk took. Every signal counted here would still have had to clear the AI gatekeeper, whole-share sizing on a $2,000 cap, the max-positions cap and the daily-loss breaker, so these counts are an UPPER BOUND on what would have been realised._

### Reading

- **Adding to winners barely moves the total and costs more per dollar risked.** Over 1,956 trades, an add at +1R or +1.5R earns about +16R more in total (+303R vs +287R): +0.155R vs +0.147R per original trade. But it risks more money to get there - $69.10 per trade at +1R and $59.53 at +1.5R, against $50.00. Per $50 risked that is +0.112R (add at +1R) and +0.130R (add at +1.5R), both below the current +0.147R.
- **Add at +1R** also lowers PF (1.30 vs 1.37) and deepens the worst drawdown (57.4R vs 42.7R): 747 adds, many of them stopped at breakeven.
- **Add at +1.5R** keeps PF (1.37) and adds 2.5R of drawdown; it pays on mean_reversion_reclaim (+16.3R from its adds) and not at all on momentum_continuation (-0.1R).
- **The account cannot hold most adds.** With the add, 34-47% of trades would exceed the $1,250 (25%) position cap, so live adds would often be trimmed or refused - the small gain above is an upper bound. (25% of trades already exceed it without an add here because this replay sizes in fractional shares at a flat $50 risk; live sizing applies the cap.)

**No change proposed** - the evidence for pyramiding is too thin to justify the extra money at risk. Study only; the live rule is unchanged.

### Standing rule (CEO 2026-10-21): NEVER AVERAGE DOWN

The worker never buys a symbol it already holds, at a loss or otherwise. Two guards enforce it: a held position goes to the management branch, which ends before the entry scan; and before any order the broker itself is asked (`position_mgmt.duplicate_entry_exists`), so a position the local record missed is still refused. `tests/test_never_average_down.py` holds both, end to end through the worker loop. Any future add-to-winner rule would add only above breakeven, never below the entry.
---

## 25. Weekly scoreboard - live R/ATR vs owner 0.5% ladder, and the switch rule (2026-10-21)

**Decision (CEO 2026-10-21).** Every Friday at 16:30 ET Discord gets the weekly scoreboard (`scoreboard.py`, launchd com.tradingbot.scoreboard): for every daily trade on the $5,000 account since 2026-10-09, closed or open, the dollars it made under the LIVE rule (the R/ATR ladder) against the owner's 0.5% ladder (BE at +0.5%, then 0.5% under the highest close in 0.25% steps) replayed on 5-minute bars from the same entry, original stop and live target. Per trade and in total.

**The rule: switch to the owner ladder if it beats the live ladder by more than $0 in total over at least 10 CLOSED trades.** The scoreboard states each week whether the rule is met; the switch itself is a config change (`daily_trailing` -> the owner ladder) made by the CEO once it is.

First reading (2026-10-21, 1 closed trade): TXN live -$6.87, owner ladder +$4.74 (+$11.61); REGN (open) live +$4.79, owner ladder $0.00 (stopped at breakeven on the 10-20 dip). Rule not met: 1 of 10 closed trades.

Method notes: live P&L is the journal's realized P&L for a closed trade and the broker's unrealized P&L for an open one; the owner-ladder result is its replayed exit, or the last 5-minute close if it would still hold. Fills: a bar opening through a level fills at its open; a bar spanning stop and target hits the stop first. The replay starts after the entry fill time, so the entry bar itself never triggers anything.

### Fix (CEO 2026-10-21): rules vs rules; the switch is the CEO's

- The table now has three money columns: **LIVE $** (what the account
  actually made - reference only), **RULE $** (the CURRENT live R/ATR ladder
  replayed on the same 5-minute bars from the same entry, original stop and
  target - `lock_replay.replay_r_atr`) and **OWNER $** (the owner's 0.5%
  ladder, replayed the same way). **DIFF = OWNER $ - RULE $**: the rule is
  judged against the rule, not against live accidents. Neither replay models
  time stops or earnings exits.
- Trades whose live result a journaled bug bent (ops events
  `fill_corrected` / entry-bug kinds) carry a dagger (TXN 10-14).
- **The switch rule now reads: switch to the owner ladder if it beats the
  live RULE by more than $0 in total over at least 10 closed trades.** The
  Friday post ends with **"CEO action: switch"** or **"CEO action: none"**.
  The decision and the config change are the CEO's; the owner never edits
  the config.
- Reading after the fix (2026-10-21): TXN† live -$6.87, rule $0.00 (the
  rule's +1R step put the stop at entry), owner +$4.74; REGN (open) rule
  +$4.91 still holding, owner $0.00. Owner - rule over 1 closed trade:
  +$4.74. CEO action: none (9 more closed trades needed).
---

## 26. Study: S44 morning-momentum (gap-and-go / opening-range breakout) replay on catalyst names (2026-10-22)

**S44 (CEO 2026-10-22, item 2) - morning-momentum setup replay, study only, no live change.** No such setup exists in the code; it is defined here as:

> - Candidates per day: catalyst names per the scanner rule, LONG side only: gap UP >= 3% (first RTH 5-min open vs previous RTH close), Finnhub company news with datetime in the 18 h before 09:30 ET, price on the open 5-1200, 20-session ADV (close x volume, IEX, prior sessions) >= $20M, top 15 by gap per day.
> - Opening range = the 09:30-09:45 ET bars (3 x 5-min). Entry: the first completed 5-min bar between 09:45 and 11:00 ET that CLOSES above the opening-range high; fill at the NEXT bar's open. Skip if that open is already above the target.
> - Stop: the opening-range low. Skip only if risk <= 0; report the median risk %.
> - Target: entry + 2R. Exit at 15:55 ET same day if neither hit (intraday only). Fills: a bar opening through a level fills at its open; a bar spanning stop and target takes the stop.
> - One trade per symbol per day; no SPY filter (report the SPY-trending vs not split as a sensitivity).

Implementation notes: the "already above the target" check uses the target planned at the signal (trigger close + kR from the trigger close to the OR low), since a target measured from the fill can never be below the fill; the traded target is fill + kR. Trigger bars start 09:45..10:55 (complete by 11:00). The 15:55 exit is the 15:55 bar's open. SPY trending = SPY close above its 20-day EMA as of the previous session.

Window 2025-10-26..2026-10-21 (51.4 weeks; Finnhub news starts 2025-10-26), 162 universe symbols. Candidates: 1289 gap-up/price/ADV symbol-days on 139 symbols, 1197 with news, 1071 after the 15/day cap; outcomes no_trigger 617, traded 454. Exits at 2R: stop 110, target 22, time 322.

| Rows | Trades | Trades/week | Win % | Expectancy (R) | PF | MaxDD (R) | Median risk % |
|---|---|---|---|---|---|---|---|
| **2R target (the setup)** | 454 | 8.83 | 49.6% | +0.014 | 1.04 | 20.7 | 3.01% |
| sensitivity: 1.5R target | 454 | 8.83 | 50.0% | +0.021 | 1.07 | 18.66 | 3.01% |
| sensitivity: 3R target | 454 | 8.83 | 49.6% | +0.015 | 1.05 | 20.44 | 3.01% |
| split @2R: SPY trending (prior close > 20d EMA) | 265 | - | 47.2% | -0.032 | 0.9 | 14.65 | 3.02% |
| split @2R: SPY not trending | 189 | - | 52.9% | +0.077 | 1.25 | 16.39 | 3.01% |

**By month (2R target).**

| Month | Trades | Trades/week | Win % | Expectancy (R) | PF | MaxDD (R) | Median risk % |
|---|---|---|---|---|---|---|---|
| 2025-10 | 11 | - | 18.2% | -0.386 | 0.21 | 4.25 | 3.16% |
| 2025-11 | 27 | - | 25.9% | -0.482 | 0.26 | 16.2 | 2.49% |
| 2025-12 | 11 | - | 36.4% | -0.204 | 0.66 | 4.96 | 3.15% |
| 2026-01 | 30 | - | 56.7% | +0.178 | 1.63 | 5.04 | 3.12% |
| 2026-02 | 31 | - | 61.3% | +0.159 | 1.56 | 3.45 | 3.84% |
| 2026-03 | 36 | - | 47.2% | +0.001 | 1.0 | 9.81 | 2.65% |
| 2026-04 | 48 | - | 45.8% | -0.075 | 0.74 | 8.33 | 2.97% |
| 2026-05 | 53 | - | 56.6% | +0.115 | 1.41 | 4.0 | 2.81% |
| 2026-06 | 53 | - | 52.8% | +0.017 | 1.06 | 7.27 | 3.02% |
| 2026-07 | 60 | - | 55.0% | +0.081 | 1.33 | 6.88 | 3.32% |
| 2026-08 | 44 | - | 50.0% | -0.061 | 0.81 | 8.92 | 3.36% |
| 2026-09 | 37 | - | 62.2% | +0.486 | 3.93 | 2.18 | 3.24% |
| 2026-10 | 13 | - | 7.7% | -0.625 | 0.01 | 8.13 | 2.05% |

**Bar** (expectancy >= +0.15R over >= 50 trades): 454 trades at +0.014R -> **not proposed**.

**Missed movers 10-19..10-21.**

| Day | Ticker | Source | Candidate | Detail | Entered | R (2R target) |
|---|---|---|---|---|---|---|
| 2026-10-19 | DDOG | cache top-10 #1 (+5.4%) | yes | gap +4.4%, news | no (no_trigger) | - |
| 2026-10-19 | AMZN | cache top-10 #2 (+5.0%) | no | gap +2.3% < +3% | no | (would-be +1.44R if it were) |
| 2026-10-19 | PLTR | cache top-10 #3 (+4.4%) | no | gap +0.5% < +3% | no | (would-be +0.34R if it were) |
| 2026-10-19 | MRNA | cache top-10 #4 (+4.3%) | no | gap +0.0% < +3% | no | (no_trigger if it were) |
| 2026-10-19 | MRVL | cache top-10 #5 (+4.1%) | yes | gap +4.7%, news | no (no_trigger) | - |
| 2026-10-19 | TSLA | cache top-10 #6 (+3.7%) | no | gap +0.8% < +3% | no | (would-be +0.28R if it were) |
| 2026-10-19 | WULF | cache top-10 #7 (+3.5%) | yes | gap +5.9%, news | no (no_trigger) | - |
| 2026-10-19 | ANET | cache top-10 #8 (+3.3%) | no | gap +3.2%; outside top 15 | no | (no_trigger if it were) |
| 2026-10-19 | ACN | cache top-10 #9 (+3.1%) | no | gap -1.4% < +3% | no | (would-be +1.46R if it were) |
| 2026-10-19 | LRCX | cache top-10 #10 (+3.1%) | yes | gap +3.9%, news | yes 09:50 @ 277.52 | -1.00R (stop) |
| 2026-10-20 | MRNA | report #1 | no | gap -0.6% < +3% | no | (no_trigger if it were) |
| 2026-10-20 | MSTR | report #2 | no | gap -1.5% < +3% | no | (would-be +2.00R if it were) |
| 2026-10-20 | MMM | report #3 | no | gap +0.7% < +3%; ADV $19M < $20M | no | (would-be +0.38R if it were) |
| 2026-10-20 | NU | report #4 | no | gap +0.8% < +3% | no | (would-be +0.24R if it were) |
| 2026-10-20 | BMY | report #5 | no | gap +1.8% < +3% | no | (would-be +0.53R if it were) |
| 2026-10-20 | NOC | report #6 | yes | gap +3.1%, news | yes 09:50 @ 542.10 | -0.47R (time 15:55) |
| 2026-10-20 | TSLL | report #7 | no | gap +1.2% < +3% | no | (would-be +0.43R if it were) |
| 2026-10-20 | AAL | report #8 | no | gap -0.4% < +3% | no | (would-be +0.26R if it were) |
| 2026-10-20 | ON | report #9 | no | gap -1.4% < +3% | no | (would-be +2.00R if it were) |
| 2026-10-20 | NXPI | report #10 | no | gap -1.2% < +3% | no | (would-be +0.96R if it were) |
| 2026-10-21 | SMCI | report #1 | yes | gap +12.2%, news | no (no_trigger) | - |
| 2026-10-21 | MSTR | report #2 | no | gap +2.7% < +3% | no | (would-be +2.00R if it were) |
| 2026-10-21 | WULF | report #3 | yes | gap +5.7%, news | no (no_trigger) | - |
| 2026-10-21 | SPCX | report #4 | yes | gap +8.6%, news | yes 09:50 @ 163.34 | -1.00R (stop) |
| 2026-10-21 | DDOG | report #5 | no | gap +1.4% < +3% | no | (would-be +0.38R if it were) |
| 2026-10-21 | ZS | report #6 | no | gap +0.4% < +3% | no | (would-be +1.29R if it were) |
| 2026-10-21 | NET | report #7 | no | gap +2.8% < +3% | no | (would-be +2.00R if it were) |
| 2026-10-21 | CRWD | report #8 | no | gap +1.5% < +3% | no | (would-be +0.95R if it were) |
| 2026-10-21 | IREN | report #9 | yes | gap +3.7%, news | no (no_trigger) | - |
| 2026-10-21 | DELL | report #10 | yes | gap +7.6%, news | no (no_trigger) | - |
| 2026-10-21 | CRWV | report: gapped on news, not in pool | yes | gap +3.6%, news | no (no_trigger) | - |

### Reading

- **Flat, not an edge.** 454 trades (8.8/week) at +0.014R, PF 1.04, MaxDD 20.7R. The target barely matters (1.5R +0.021R, 3R +0.015R): at 2R only 22 trades reached the target, 110 hit the OR-low stop and 322 (71%) were closed at 15:55.
- **The news test hardly filters.** 1,197 of 1,289 gap-up/price/ADV symbol-days had Finnhub news in the 18 h window; this universe is large caps that always have headlines. 617 of the 1,071 capped candidates never closed above the OR high by 11:00.
- **Months swing.** 7 of 13 months positive; 2026-09 +0.486R (37 trades), 2025-11 -0.482R (27), 2026-10 so far -0.625R (13). SPY split: not trending +0.077R (189), trending -0.032R (265) - post-hoc, and neither side clears the bar.
- **Missed movers.** 10-19 has no report: its list is the top 10 universe gainers by close vs prior close from the 5-min cache. Of 31 name-days (incl. CRWV on 10-21), 10 were candidates and 3 entered: LRCX 10-19 -1.00R, NOC 10-20 -0.47R, SPCX 10-21 -1.00R (-2.47R together). Most movers did not gap >= +3% (their move came after the open), so a gap-and-go cannot see them by construction. On 10-21: SMCI was a candidate (+12.2% gap, news) but faded below its OR high and never triggered; DDOG (+1.4% gap), ZS (+0.4%), NET (+2.8%) and CRWD (+1.5%) were not candidates. "Would-be" R in the table is the setup run on a name that failed the candidate test, for information only.
- **Bar:** +0.014R over 454 trades is below +0.15R -> **not proposed**. Catalyst names stay WATCH-ONLY (agenda 23).

### Limits

- Today's universe (survivorship): the names are today's 162-name universe, not each day's; names that left the list are missing.
- IEX 5-minute bars (the live feed): IEX volume is a slice of the tape, bars can be missing and the 09:30 print can differ from the consolidated open, so the gap, OR and ADV are IEX-based.
- Finnhub free tier: about one year of company news, so coverage starts ~2025-10-26; the news test is Finnhub's view only (no PR wires, no earnings calendar).
- No gatekeeper, no whole-share sizing, no slippage or spread on the next-bar-open fill, no max-positions or daily-loss breaker: an upper bound on what the desk would have realised.

Reproduce with `python study_exit_grid.py --s44`.

