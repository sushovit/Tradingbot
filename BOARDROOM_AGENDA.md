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
