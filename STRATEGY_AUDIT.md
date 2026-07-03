# Strategy Playbook Audit

Coverage of the trading playbook: every setup, where it lives, how it's
verified, and where it's enabled. All signals flow through the same pipeline:
**detector → AI gatekeeper → risk validation (R:R ≥ 1.5, sizing, max
positions, circuit breaker) → broker bracket order → journal.** No strategy
bypasses it.

| Playbook setup | File | Stop = thesis invalidation | Tests (tests/test_strategies.py) | Enabled tickers |
|---|---|---|---|---|
| Trend continuation (EMA golden cross + ADX + RSI + volume) | `strategies/trend_continuation.py` | ATR-based below entry (approximates slow EMA / swing low; playbook-approved) | textbook fire ✅ · chop counterexample ✅ · stop/target placement ✅ · ADX filter pass-journal ✅ | NVDA, TSLA, AMD |
| Momentum continuation (20-bar-high breakout, 1.5× volume, +3%) | `strategies/momentum_continuation.py` | Below breakout bar low | textbook fire ✅ · chop counterexample ✅ · stop at breakout low ✅ · ≥2R target ✅ · volume filter pass-journal ✅ | NVDA, TSLA |
| Mean-reversion reclaim (≥10% washout, close > prior high + EMA9, 1.2× volume) | `strategies/mean_reversion_reclaim.py` | Below reclaim bar low | textbook fire ✅ · no-washout counterexample ✅ · stop at reclaim low ✅ · volume filter pass-journal ✅ | AMD |
| Event / flow (index inclusions, scheduled catalysts) | `orders.py` (order sheets only — cannot be auto-detected) | CEO-provided stop, mandatory; forced close at `hard_exit_date` | missing hard_exit_date rejected ✅ · missing stop rejected ✅ (tests/test_risk_rules.py) | via order sheet |

## Downstream pipeline verification

| Gate | Where | Test |
|---|---|---|
| R:R < 1.5 rejected | `risk.check_signal` | `test_low_rr_signal_rejected_downstream` ✅ |
| Rejected signals journaled as passes (source="rules") | `journal.log_rules_pass` | `test_rejected_signals_are_journaled_as_passes` ✅ |
| Per-ticker strategy enablement | `bot_config.json` `"strategies"` | `test_per_ticker_strategy_config` ✅ |
| Notional > 30% equity rejected | `risk.check_signal` | `test_oversized_notional_rejected` ✅ |
| Max positions enforced | `risk.check_signal` | `test_max_positions_rejected` ✅ |

Run `python -m pytest tests -q` to re-verify (33 tests, no network).
