# Backtest report
2026-07-25 11:05 ET  |  2026-07-25 20:50 Nepal  |  US market: CLOSED (opens Mon 19:15 Nepal)

Universe: 48 tickers | 3y daily bars | base: 1% risk on $2,000, 25% cap, whole shares, next-bar-open entries, bracket exits

## Headline verdicts
- **trend_continuation**: POSITIVE expectancy (+0.31R) in trending regime (26 trades), NEGATIVE (-0.14R) in chop (7 trades).
- **momentum_continuation**: POSITIVE expectancy (+0.19R) in trending regime (355 trades), NEGATIVE (-0.00R) in chop (47 trades).
- **mean_reversion_reclaim**: POSITIVE expectancy (+0.25R) in trending regime (719 trades), POSITIVE expectancy (+0.38R) in chop (205 trades).

## Per strategy x regime (base config, 2R target)
| Strategy | Regime | Trades | Win% | Avg R | Expectancy | PF | MaxDD (R) | Avg $ |
|---|---|---|---|---|---|---|---|---|
| trend_continuation | trending | 26 | 50.0 | 0.311 | 0.311 | 1.45 | 4.78 | 6.16 |
| trend_continuation | chop | 7 | 28.6 | -0.143 | -0.143 | 0.8 | 5.0 | -2.67 |
| momentum_continuation | trending | 355 | 40.0 | 0.186 | 0.186 | 1.29 | 14.73 | 2.2 |
| momentum_continuation | chop | 47 | 31.9 | -0.002 | -0.002 | 1.0 | 9.11 | -0.86 |
| mean_reversion_reclaim | trending | 719 | 40.9 | 0.246 | 0.246 | 1.39 | 13.87 | 3.63 |
| mean_reversion_reclaim | chop | 205 | 45.9 | 0.376 | 0.376 | 1.62 | 9.32 | 6.4 |

## Deterministic filter rejections (base pass)
- volume_low: 6059
- below_ema9: 1232
- adx_low: 699
- gap_below_reclaim_mid: 512
- change_too_small: 270
- invalid_stop: 118
- size_zero: 27
- rsi_low: 24
- price_too_high_for_account: 24
- stop_above_entry_at_open: 8

## Sensitivity: ADX threshold (trend_continuation)
| ADX >= | Trades | Win% | Expectancy (R) | PF |
|---|---|---|---|---|
| 20 | 143 | 37.8 | 0.077 | 1.11 |
| 25 | 64 | 40.6 | 0.13 | 1.19 |
| 28 | 33 | 45.5 | 0.215 | 1.31 |
| 30 | 18 | 44.4 | 0.336 | 1.61 |

## Sensitivity: target R (all strategies, base signals)
| Target | Strategy | Trades | Win% | Expectancy (R) | PF |
|---|---|---|---|---|---|
| 1.5R | trend_continuation | 33 | 48.5 | 0.068 | 1.1 |
| 1.5R | momentum_continuation | 424 | 42.9 | 0.078 | 1.13 |
| 1.5R | mean_reversion_reclaim | 1005 | 47.5 | 0.194 | 1.34 |
| 2.0R | trend_continuation | 33 | 45.5 | 0.215 | 1.31 |
| 2.0R | momentum_continuation | 402 | 39.1 | 0.164 | 1.25 |
| 2.0R | mean_reversion_reclaim | 924 | 42.0 | 0.275 | 1.44 |
| 3.0R | trend_continuation | 31 | 38.7 | 0.431 | 1.57 |
| 3.0R | momentum_continuation | 373 | 33.2 | 0.324 | 1.45 |
| 3.0R | mean_reversion_reclaim | 785 | 34.4 | 0.379 | 1.53 |

## Sensitivity: stop placement (ATR mult, trend_continuation; momentum + reclaim are bar-low natively)
| Stop mode | Trades | Win% | Expectancy (R) |
|---|---|---|---|
| ATR x1.5 | 33 | 36.4 | -0.085 |
| ATR x2.0 | 33 | 45.5 | 0.215 |
