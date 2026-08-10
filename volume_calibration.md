# Volume-threshold calibration
2026-08-10 10:31 ET  |  2026-08-10 20:16 Nepal  |  US market: OPEN (closes in 5h28m)

Universe: 48 tickers | 3y daily bars | 3R target, next-bar-open entry, 1% risk on $2,000

RESEARCH ONLY — rule changes require mini-boardroom ratification.

## Volume-ratio distribution by calendar month
_bar volume / trailing 20-bar average. The share clearing each multiplier is the direct test of the seasonal-thinness hypothesis._
| Month | Bars | Median ratio | % >= 1.2x | % >= 1.3x | % >= 1.5x |
|---|---|---|---|---|---|
| Jan | 2928 | 1.04 | 35.9% | 29.2% | 18.6% |
| Feb | 2784 | 0.88 | 23.6% | 19.5% | 12.9% |
| Mar | 3024 | 0.90 | 24.3% | 18.7% | 12.0% |
| Apr | 3072 | 0.88 | 25.7% | 21.1% | 14.5% |
| May | 3024 | 0.84 | 21.3% | 17.0% | 11.2% |
| Jun | 2880 | 0.95 | 26.8% | 20.9% | 13.0% |
| Jul | 2928 | 0.88 | 23.0% | 18.6% | 12.4% |
| Aug | 2880 | 0.82 | 22.4% | 17.6% | 12.0% |
| Sep | 2928 | 1.01 | 33.8% | 27.4% | 17.2% |
| Oct | 3264 | 0.93 | 28.2% | 22.5% | 15.0% |
| Nov | 2880 | 0.90 | 25.0% | 19.8% | 12.4% |
| Dec | 3024 | 0.82 | 21.1% | 16.9% | 10.9% |

**Jul-Aug vs rest of year**: median ratio 0.851 vs 0.914; share clearing 1.5x 12.2% vs 13.8%.
**Seasonal-thinness hypothesis: NOT SUPPORTED — summer bars clear the multipliers at a similar rate; the ratio is self-normalising against its own trailing average.**

## momentum_continuation — threshold sweep (live: 1.5x)
| Volume >= | Trades | Win% | Expectancy (R) | PF | MaxDD (R) |
|---|---|---|---|---|---|
| 1.0x | 533 | 33.0 | 0.313 | 1.43 | 18.73 |
| 1.2x | 472 | 33.7 | 0.344 | 1.48 | 18.74 |
| 1.3x | 435 | 33.8 | 0.353 | 1.5 | 17.73 |
| 1.5x | 373 | 33.2 | 0.324 | 1.45 | 15.73 |  <-- LIVE

**Expectancy-optimal threshold: 1.3x** (+0.353R)

### momentum_continuation by calendar month
| Month | 1.0x trades / exp | 1.2x trades / exp | 1.3x trades / exp | 1.5x trades / exp |
|---|---|---|---|---|
| Jan | 49 / 0.503 | 43 / 0.632 | 42 / 0.671 | 37 / 0.389 |
| Feb | 41 / 0.379 | 39 / 0.282 | 38 / 0.316 | 32 / 0.199 |
| Mar | 31 / -0.163 | 28 / -0.359 | 26 / -0.309 | 24 / -0.252 |
| Apr | 36 / 0.59 | 29 / 0.56 | 27 / 0.379 | 20 / 0.435 |
| May | 70 / 0.169 | 61 / 0.294 | 53 / 0.3 | 47 / 0.205 |
| Jun | 49 / 0.034 | 44 / 0.108 | 36 / 0.022 | 29 / -0.01 |
| Jul | 41 / -0.3 | 36 / -0.421 | 32 / -0.473 | 31 / -0.456 |
| Aug | 17 / 0.433 | 15 / 0.64 | 13 / 0.585 | 13 / 0.585 |
| Sep | 45 / 0.676 | 42 / 0.796 | 39 / 0.934 | 32 / 1.107 |
| Oct | 50 / 0.265 | 47 / 0.346 | 47 / 0.346 | 39 / 0.418 |
| Nov | 51 / 0.898 | 43 / 0.986 | 39 / 1.092 | 34 / 1.164 |
| Dec | 53 / 0.23 | 45 / 0.199 | 43 / 0.157 | 35 / 0.092 |

**At the live 1.5x**: Jul-Aug 44 trades (-0.149R), rest of year 329 (0.387R).

## mean_reversion_reclaim — threshold sweep (live: 1.2x)
| Volume >= | Trades | Win% | Expectancy (R) | PF | MaxDD (R) |
|---|---|---|---|---|---|
| 1.0x | 1022 | 33.8 | 0.354 | 1.49 | 23.48 |
| 1.2x | 785 | 34.4 | 0.379 | 1.53 | 18.0 |  <-- LIVE
| 1.3x | 696 | 34.6 | 0.397 | 1.56 | 18.0 |
| 1.5x | 541 | 34.6 | 0.392 | 1.57 | 15.1 |

**Expectancy-optimal threshold: 1.3x** (+0.397R)

### mean_reversion_reclaim by calendar month
| Month | 1.0x trades / exp | 1.2x trades / exp | 1.3x trades / exp | 1.5x trades / exp |
|---|---|---|---|---|
| Jan | 104 / 0.328 | 75 / 0.379 | 70 / 0.278 | 53 / 0.45 |
| Feb | 87 / 0.126 | 69 / 0.086 | 61 / 0.1 | 42 / 0.226 |
| Mar | 76 / -0.301 | 53 / -0.194 | 39 / -0.194 | 31 / -0.393 |
| Apr | 95 / 0.905 | 82 / 0.923 | 74 / 0.991 | 56 / 1.194 |
| May | 86 / 0.684 | 70 / 0.433 | 60 / 0.564 | 50 / 0.178 |
| Jun | 76 / 0.4 | 68 / 0.339 | 61 / 0.099 | 47 / 0.207 |
| Jul | 60 / 0.075 | 42 / -0.044 | 35 / 0.159 | 34 / 0.092 |
| Aug | 61 / 0.095 | 41 / 0.4 | 34 / 0.462 | 25 / 0.508 |
| Sep | 96 / 0.185 | 77 / 0.1 | 71 / 0.225 | 53 / 0.219 |
| Oct | 87 / 0.575 | 70 / 0.494 | 66 / 0.464 | 55 / 0.542 |
| Nov | 108 / 0.546 | 80 / 0.632 | 71 / 0.623 | 52 / 0.903 |
| Dec | 86 / 0.32 | 58 / 0.637 | 54 / 0.616 | 43 / 0.023 |

**At the live 1.2x**: Jul-Aug 83 trades (0.175R), rest of year 702 (0.403R).
