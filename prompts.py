"""
prompts.py — single source of truth for the gatekeeper prompts.

Both the Claude gatekeeper (claude_integration.py) and the local analyst
(local_analyst.py) build their prompts here so the two models always judge a
setup on identical information.
"""

GATEKEEPER_REQUIRED_KEYS = [
    "approved", "conviction_score", "market_regime", "crossover_quality",
    "rejection_reason", "key_risk", "reasoning",
]

GATEKEEPER_SYSTEM_PROMPT = (
    "You are a senior quantitative trader acting as the final risk gatekeeper for an automated "
    "intraday trading system. A trade signal has been detected. Your job is to determine "
    "whether this setup has genuine trading edge or is a false signal to be avoided.\n\n"
    "Be rigorously skeptical. In intraday trading, the majority of technical signals are false "
    "signals caused by choppy price action. Only approve setups where multiple independent "
    "factors confirm genuine momentum.\n\n"
    "Return ONLY a valid JSON object — no markdown, no extra text."
)

# Appended for the local model only. It is an OBSERVED junior analyst, not a
# subordinate: its verdicts are journaled and compared against the senior
# gatekeeper's, never used to control trades. It is not penalized for
# disagreeing — it is penalized for confident wrongness.
JUNIOR_ANALYST_ADDENDUM = (
    "ROLE CONTEXT: You are a junior analyst whose verdicts are journaled and "
    "compared against a senior gatekeeper's decisions over time. Your verdict "
    "does not control the trade. You are NOT rewarded for agreeing with the "
    "senior gatekeeper and NOT penalized for disagreeing — you are penalized "
    "only for being confidently wrong. If the evidence is ambiguous or you "
    "are unsure, say so: state the uncertainty in your reasoning and lower "
    "your conviction_score rather than guessing with false confidence."
)


def get_system_prompt(role: str = "gatekeeper") -> str:
    """Role-specific system prompt. 'gatekeeper' (Claude) is unchanged;
    'junior_analyst' (local model) gets the observed-analyst framing."""
    if role == "junior_analyst":
        return GATEKEEPER_SYSTEM_PROMPT + "\n\n" + JUNIOR_ANALYST_ADDENDUM
    return GATEKEEPER_SYSTEM_PROMPT


def build_gatekeeper_user_prompt(
    ticker: str,
    candle_data_str: str,
    adx_val: float,
    rsi_val: float,
    ema_spread_pct: float,
    volume_trend: str,
    crossover_count: int,
    dist_to_resistance_pct: float,
    entry_price: float,
    stop_price: float,
    target_price: float,
    rr_ratio: float,
    interval_mins: int,
    fast_ema: int,
    slow_ema: int,
    news_str: str,
    setup_name: str = "trend_continuation",
    setup_description: str = None,
) -> str:
    setup_description = setup_description or (
        f"EMA{fast_ema} crossed above EMA{slow_ema} on {interval_mins}-minute chart"
    )
    return f"""TRADE SETUP UNDER REVIEW: {ticker}
Setup type: {setup_name}
Setup: {setup_description}

=== TECHNICAL CONTEXT (last 20 candles) ===
{candle_data_str}

=== KEY DERIVED METRICS ===
- ADX: {adx_val:.1f} (>25 = trending, <20 = ranging/avoid)
- RSI: {rsi_val:.1f} (ideal: 50-70; >75 = overextended; <45 = weak momentum)
- EMA Spread: {ema_spread_pct:.2f}% (higher = stronger trend separation)
- Volume Trend: {volume_trend} (increasing = confirmation; decreasing = suspect)
- EMA Crossovers in last 20 candles: {crossover_count} (>2 = choppy/whipsaw zone)
- Distance to 20-bar High (resistance): {dist_to_resistance_pct:.1f}%
- Proposed Trade: Entry={entry_price:.2f} | Stop={stop_price:.2f} | Target={target_price:.2f} | RR={rr_ratio:.1f}:1

=== RECENT NEWS HEADLINES (last 5 days) ===
{news_str}

=== DECISION RULES ===
APPROVE only if ALL of:
  1. ADX > 25 (trending market, not ranging)
  2. Fewer than 3 EMA crossovers in prior 20 candles (clean trend, not whipsaw)
  3. Volume is increasing or neutral on the signal candle
  4. Distance to resistance > 1.5% (target has clear path)
  5. RSI between 45 and 75 (momentum present, not overextended)
  6. No earnings announcement or major analyst downgrade in news

REJECT if ANY of:
  - ADX < 20 (market is ranging — the signal is meaningless)
  - 3+ crossovers in 20 candles (classic whipsaw pattern)
  - Volume declining AND RSI < 50 (weak, low-conviction move)
  - Distance to resistance < 0.5% (price is immediately at resistance)
  - RSI > 75 (overextended at entry, poor risk/reward)
  - News contains: earnings report, analyst downgrade, negative guidance

Return JSON with exactly these keys:
{{
  "approved": true or false,
  "conviction_score": integer 0-100,
  "market_regime": "Trending" or "Ranging" or "Volatile",
  "crossover_quality": "Clean" or "Choppy",
  "rejection_reason": null or one-sentence reason string,
  "key_risk": brief description of the main risk in this setup,
  "reasoning": one comprehensive sentence explaining the decision
}}"""
