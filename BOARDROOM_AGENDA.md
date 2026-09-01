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
