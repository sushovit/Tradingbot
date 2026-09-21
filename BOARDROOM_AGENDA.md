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
09-21. Stats split at 09-21. Each item: what / evidence / change / how we'd
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
