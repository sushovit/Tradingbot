# Backtest report
2026-07-26 11:01 ET  |  2026-07-26 20:46 Nepal  |  US market: CLOSED (opens Mon 19:15 Nepal)

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

## Short lane — RESEARCH ONLY (not wired live)
_orders.py still rejects SELL-to-open. A boardroom reviews these numbers before any live short exists._
| Strategy | Regime | Trades | Win% | Avg R | Expectancy | PF | MaxDD (R) |
|---|---|---|---|---|---|---|---|
| breakdown_continuation | trending | 150 | 31.3 | -0.118 | -0.118 | 0.84 | 29.63 |
| breakdown_continuation | chop | 200 | 18.0 | -0.511 | -0.511 | 0.42 | 104.79 |
| failed_reclaim | trending | 811 | 30.9 | -0.098 | -0.098 | 0.87 | 97.63 |
| failed_reclaim | chop | 557 | 27.3 | -0.186 | -0.186 | 0.76 | 107.01 |
- **breakdown_continuation**: NEGATIVE (-0.12R) in trending regime (150 trades), NEGATIVE (-0.51R) in chop (200 trades).
- **failed_reclaim**: NEGATIVE (-0.10R) in trending regime (811 trades), NEGATIVE (-0.19R) in chop (557 trades).
