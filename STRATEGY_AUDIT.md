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

## Playbook rules

| # | Rule | Where enforced | Test |
|---|---|---|---|
| 1 | Stop = thesis invalidation, never a fixed % | every strategy's `detect()` | stop-placement tests ✅ |
| 2 | Same downstream pipeline for every signal (gatekeeper → risk → bracket → journal) | `live_bot_worker`, `orders.py` | pipeline tests ✅ |
| 3 | **Gap-abort (from the 2026-07-10 MRVL loss):** a reclaim entry is invalid if the next session opens below the reclaim bar's midpoint. Bot side: `mean_reversion_reclaim` rejects with `gap_below_reclaim_mid`. Sheet side: optional `abort_if_open_below` field — price below it at ingest ⇒ reject + journal as rules pass (unverifiable price ⇒ protective reject). | `strategies/mean_reversion_reclaim.py`, `orders.ingest` | `test_reclaim_aborts_on_gap_below_midpoint`, `test_ingest_gap_abort_rejects_and_journals_pass` ✅ |

## Downstream pipeline verification

| Gate | Where | Test |
|---|---|---|
| R:R < 1.5 rejected | `risk.check_signal` | `test_low_rr_signal_rejected_downstream` ✅ |
| Rejected signals journaled as passes (source="rules") | `journal.log_rules_pass` | `test_rejected_signals_are_journaled_as_passes` ✅ |
| Per-ticker strategy enablement | `bot_config.json` `"strategies"` | `test_per_ticker_strategy_config` ✅ |
| Notional > 30% equity rejected | `risk.check_signal` | `test_oversized_notional_rejected` ✅ |
| Max positions enforced | `risk.check_signal` | `test_max_positions_rejected` ✅ |
| Hard capital cap (broker $97k → $1,000) | `risk.effective_equity` | `test_effective_equity_caps_broker_balance` ✅ |
| No margin: total notional ≤ cash | `risk.check_signal` | `test_total_notional_cannot_exceed_cash` ✅ |
| Whole-share reality journaled | `risk.zero_size_reason` | `test_whole_share_rejection_is_journaled` ✅ |
| Universe filters (price/volume/OTC/ETF) + ranking | `universe.filter_and_rank` | `test_universe_*` ✅ |
| Exit sync idempotent, PnL vs journaled BUY, outcome linked | `orders.sync` | `tests/test_sync.py` ✅ |
| Core-watch setup flags (pre-breakout / washout) + merge + cap 20 | `universe.classify_core_setup`, `merge_candidates` | `test_classify_*`, `test_merge_*`, `test_combined_output_capped_at_20` ✅ |

Run `python -m pytest tests -q` to re-verify (56 tests, no network).
