"""
smoke_test_gatekeeper.py — one-shot end-to-end check of the gatekeeper path:

    .env -> claude_integration.py -> Anthropic API -> JSON verdict -> journal

Exercises the SAME code path the bot loop uses (prompts.py, retry logic,
JSON validation in claude_integration.get_gatekeeper_decision) — this is not
a hand-rolled API request. The verdict is journaled with source="smoke_test"
so it never pollutes real decision analytics.

If Ollama is running, the local analyst is called once with the identical
context to verify shadow wiring (verdict + agreement flag). If Ollama is
down that step is skipped gracefully.

Total API usage: 1 Claude call (+1 local call if Ollama is up). Never loops.

    python smoke_test_gatekeeper.py     -> PASS / FAIL <reason>
"""

import sys

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

import journal                    # noqa: E402
import claude_integration         # noqa: E402
import local_analyst              # noqa: E402
from prompts import GATEKEEPER_REQUIRED_KEYS  # noqa: E402


def build_signal_df():
    """Synthetic but realistic 20 bars: steady uptrend, EMA9 > EMA21, healthy
    RSI/ADX, volume expanding — a textbook trend_continuation context."""
    rows = 20
    closes = [58.0 + i * 0.17 for i in range(rows)]          # 58.00 -> 61.23
    highs = [c + 0.20 for c in closes]
    highs[3] = 62.50                                          # prior swing high
    return pd.DataFrame({
        "open": [c - 0.10 for c in closes],
        "high": highs,
        "low": [c - 0.25 for c in closes],
        "close": closes,
        "volume": [80_000 + i * 2_500 for i in range(rows)],
        "ema_fast": [c - 0.15 for c in closes],
        "ema_slow": [c - 0.45 for c in closes],
        "rsi_14": [55.0 + i * 0.35 for i in range(rows)],
        "adx_14": [27.0 + i * 0.25 for i in range(rows)],
    })


GK_KWARGS = {
    "ticker": "FCX",
    "df20": build_signal_df(),
    "ema_spread_pct": 0.49,
    "volume_trend": "increasing",
    "crossover_count": 1,
    "dist_to_resistance_pct": 2.1,
    "entry_price": 61.20,
    "stop_price": 59.60,       # risk  $1.60
    "target_price": 64.40,     # reward $3.20 -> R:R 2.0
    "rr_ratio": 2.0,
    "interval_mins": 5,
    "fast_ema": 9,
    "slow_ema": 21,
    "news_headlines": [
        "Freeport-McMoRan upgraded to Overweight as copper hits four-month high",
    ],
    "setup_name": "trend_continuation",
}

DECISION_CONTEXT = {
    "smoke_test": True,
    "setup": "trend_continuation",
    "entry": GK_KWARGS["entry_price"],
    "stop": GK_KWARGS["stop_price"],
    "target": GK_KWARGS["target_price"],
}


def fail(reason: str) -> int:
    print(f"\nFAIL: {reason}")
    return 1


def main() -> int:
    journal.init_db()

    # ---- 1. Claude gatekeeper through the real bot code path ----
    print("Calling Claude gatekeeper (claude_integration.get_gatekeeper_decision)...")
    verdict = claude_integration.get_gatekeeper_decision(**GK_KWARGS)

    if "error" in verdict:
        return fail(f"gatekeeper returned error: {verdict['error']}")
    missing = [k for k in GATEKEEPER_REQUIRED_KEYS if k not in verdict]
    if missing:
        return fail(f"verdict missing required keys: {missing}")

    print("\n--- Claude verdict ---")
    for key in ("approved", "conviction_score", "market_regime",
                "crossover_quality", "rejection_reason", "key_risk", "reasoning"):
        print(f"  {key}: {verdict.get(key)}")

    decision_id = journal.log_decision(GK_KWARGS["ticker"], "trend_continuation",
                                       DECISION_CONTEXT, verdict,
                                       source="smoke_test")
    print(f"\nJournaled as decision #{decision_id} (source=smoke_test)")

    # ---- 2. Shadow wiring, only if Ollama is actually up ----
    ollama_up = False
    try:
        requests.get(f"{local_analyst.OLLAMA_URL}/api/tags", timeout=2)
        ollama_up = True
    except requests.RequestException:
        pass

    if not ollama_up:
        print("shadow: skipped (Ollama not running)")
    else:
        print(f"\nCalling local analyst ({local_analyst.LOCAL_MODEL}) with the same context...")
        local_verdict = local_analyst.get_gatekeeper_decision(**GK_KWARGS)
        if "error" in local_verdict:
            print(f"shadow: local analyst error (graceful): {local_verdict['error']}")
        else:
            agreement = bool(local_verdict.get("approved")) == bool(verdict.get("approved"))
            print("--- Local verdict ---")
            print(f"  approved: {local_verdict.get('approved')}")
            print(f"  conviction_score: {local_verdict.get('conviction_score')}")
            print(f"  reasoning: {local_verdict.get('reasoning')}")
            print(f"  agreement with Claude: {agreement}")
            shadow_context = dict(DECISION_CONTEXT,
                                  claude_approved=bool(verdict.get("approved")),
                                  claude_conviction=verdict.get("conviction_score"))
            journal.log_decision(GK_KWARGS["ticker"], "trend_continuation",
                                 shadow_context, local_verdict,
                                 source="smoke_test", agreement=agreement)

    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
