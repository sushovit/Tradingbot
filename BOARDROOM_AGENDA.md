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
