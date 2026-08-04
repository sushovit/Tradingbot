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


# The intern desk is a DIFFERENT job from shadow-gatekeeping: an independent
# daily scan, graded by the CEO on reasoning quality, not on agreement with
# anyone. Same honesty contract: uncertainty stated beats confident wrongness.
INTERN_DESK_ADDENDUM = (
    "ROLE CONTEXT: You are a junior analyst producing your OWN independent "
    "daily scan. Nobody trades on your word automatically — your calls are "
    "recorded and graded later by a senior trader on the QUALITY OF YOUR "
    "REASONING and whether your risk framing was honest, not on whether you "
    "agreed with anyone. A 'no_trade' with a clear reason is a perfectly "
    "good answer. If the evidence is mixed, say so and lower your "
    "conviction. You are NOT penalized for a confident YES that is "
    "well-reasoned; you are penalized for scores that don't match your "
    "stated reasoning — in either direction. Every "
    "long_setup or short_setup MUST state its invalidation level (the price "
    "where the idea is wrong) — an idea without an invalidation is ungradeable."
)

INTERN_DESK_REQUIRED_KEYS = ["stance", "setup_name", "conviction",
                             "invalidation", "key_risk", "reasoning"]

# Bump when the intern prompt's SEMANTICS change, so training-data exports
# can segment rows. v1: no_trade conviction implicitly 0. v2 (2026-07-18):
# conviction = confidence in the STATED stance (no_trade included), new
# anchor ladder, reasoning-first derivation, rebalanced honesty framing.
# v3 (2026-07-26): ADX rubric verbatim, mandatory numeric invalidation
# (downgrade to no_trade without one), ticker-specific numbers required in
# reasoning, mid-band (40-50) relative-ranking second pass.
INTERN_PROMPT_VERSION = 3


def build_intern_desk_prompt(ticker: str, candle_data_str: str,
                             metrics_str: str, news_str: str) -> str:
    return f"""DAILY SCAN — {ticker} (daily bars, most recent last)

=== LAST 20 DAILY CANDLES ===
{candle_data_str}

=== DERIVED METRICS ===
{metrics_str}

=== RECENT HEADLINES ===
{news_str}

Assess this ticker for the NEXT few sessions. Playbook setups you may cite:
trend_continuation, momentum_continuation, mean_reversion_reclaim — or null
if none applies.

ADX RUBRIC (use it verbatim): ADX below 20 = no trend; 20-25 = weak trend;
25-40 = trending; above 40 = strong trend. NEVER describe a value above 25
as low.

INVALIDATION IS MANDATORY: every long_setup or short_setup MUST state a
numeric invalidation price. If you cannot name one, the idea is not
tradeable — downgrade it to no_trade yourself.

DIFFERENTIATION: your reasoning must cite at least one ticker-specific
number (a price level, an indicator value, or a volume figure). Writing
identical sentences for different tickers is a violation.

CONVICTION CALIBRATION — score conviction 0-100 using the FULL range.
Anchors:
  15 = barely a setup, mostly noise
  30 = setup exists but weak or contra-regime
  50 = valid setup with meaningful doubts
  60 = decent setup, several concerns
  70 = good setup, one clear concern
  80 = strong setup, minor concerns
  90+ = exceptional confluence, rare
Your score must FOLLOW from your reasoning: state reasoning first, then
derive the number. The anchors are reference points, NOT a menu — almost
every honest score falls BETWEEN anchors (e.g. 43, 58, 67, 77, 82).
Two different tickers should rarely receive identical scores; if your last
few scores look alike, re-derive this one from its own reasoning.
no_trade verdicts carry your real conviction IN the no-trade call, scored
on the same ladder applied to your confidence that standing aside is
correct — never auto-zero.
You are not penalized for a confident YES that is well-reasoned; you are
penalized for scores that don't match your stated reasoning — in either
direction.

Return JSON with exactly these keys, in this order:
{{
  "stance": "long_setup" or "short_setup" or "no_trade",
  "setup_name": "trend_continuation"|"momentum_continuation"|"mean_reversion_reclaim"|null,
  "invalidation": price level where the idea is wrong, or null for no_trade,
  "key_risk": one short phrase,
  "reasoning": at most 3 sentences,
  "conviction": integer 0-100 (calibrated per the anchors above, scored AFTER reasoning)
}}"""


def get_system_prompt(role: str = "gatekeeper") -> str:
    """Role-specific system prompt. 'gatekeeper' (Claude) is unchanged;
    'junior_analyst' (local shadow) gets the observed-analyst framing;
    'intern_desk' gets the independent-scan framing."""
    if role == "junior_analyst":
        return GATEKEEPER_SYSTEM_PROMPT + "\n\n" + JUNIOR_ANALYST_ADDENDUM
    if role == "intern_desk":
        return GATEKEEPER_SYSTEM_PROMPT + "\n\n" + INTERN_DESK_ADDENDUM
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

=== ADX RUBRIC (use it verbatim) ===
ADX below 20 = no trend; 20-25 = weak trend; 25-40 = trending; above 40 =
strong trend. NEVER describe a value above 25 as low, and NEVER call a
market "ranging" when ADX is above 25 — that is a contradiction.

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
