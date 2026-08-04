# Reclaim calibration — washouts deeper than 25%
2026-08-04 10:50 ET  |  2026-08-04 20:35 Nepal  |  US market: OPEN (closes in 5h09m)

Universe: 48 tickers | 3y daily bars | 3R target, stop at signal-bar low, next-bar-open entry, 1% risk on $2,000

RESEARCH ONLY — no live wiring. Rule changes require boardroom ratification.

## Variants
- **current**: close > prior-day high AND close > EMA9 (live rule)
- **a_ema9_or_two_closes**: (a) EMA9 rule OR two consecutive closes above prior-day highs, whichever first
- **b_ema5**: (b) close > prior-day high AND close > 5-day EMA

## Overall (all regimes)
| Variant | Trades | Win% | Expectancy (R) | PF | MaxDD (R) | Median bars from bounce low | No-room % |
|---|---|---|---|---|---|---|---|
| current | 218 | 32.6 | 0.059 | 1.06 | 67.12 | 15.0 | 42.2% |
| a_ema9_or_two_closes | 221 | 32.6 | 0.06 | 1.06 | 65.48 | 15.0 | 41.6% |
| b_ema5 | 248 | 31.0 | 0.08 | 1.08 | 61.29 | 15.0 | 37.1% |

## By regime
| Variant | Regime | Trades | Win% | Expectancy (R) | PF | Median bars from low | No-room % |
|---|---|---|---|---|---|---|---|
| current | trending | 178 | 28.1 | 0.092 | 1.12 | 17.0 | 48.9% |
| current | chop | 40 | 52.5 | -0.088 | 0.95 | 3.0 | 12.5% |
| a_ema9_or_two_closes | trending | 180 | 28.3 | 0.098 | 1.12 | 16.0 | 48.3% |
| a_ema9_or_two_closes | chop | 41 | 51.2 | -0.11 | 0.94 | 3.0 | 12.2% |
| b_ema5 | trending | 193 | 26.4 | 0.036 | 1.04 | 16.0 | 45.1% |
| b_ema5 | chop | 55 | 47.3 | 0.234 | 1.16 | 2.0 | 9.1% |

## The 'no room left' failure (signal completes within 2% of the 20-bar high)
| Variant | No-room trades | No-room % | Expectancy WITH room | Expectancy NO room |
|---|---|---|---|---|
| current | 92 | 42.2% | -0.05 | 0.208 |
| a_ema9_or_two_closes | 92 | 41.6% | -0.046 | 0.208 |
| b_ema5 | 92 | 37.1% | 0.004 | 0.208 |

## Arrival lateness (bars from bounce low to signal)
| Variant | Median | Mean |
|---|---|---|
| current | 15.0 | 13.2 |
| a_ema9_or_two_closes | 15.0 | 13.0 |
| b_ema5 | 15.0 | 12.0 |
