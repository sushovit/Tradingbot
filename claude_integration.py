import anthropic
import os
import json
import re
import time
import logging
from datetime import datetime, timedelta

from prompts import (
    GATEKEEPER_SYSTEM_PROMPT,
    build_gatekeeper_user_prompt,
)

# --- Configuration ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
logger = logging.getLogger(__name__)


# =============================================================================
# CORE API HELPER
# =============================================================================

def call_claude_api(system_prompt: str, user_prompt: str, max_tokens: int = 1024) -> dict:
    """Calls Claude claude-sonnet-4-6 synchronously with retry logic. Returns a parsed dict."""
    if not ANTHROPIC_API_KEY or not claude_client:
        return {"error": "ANTHROPIC_API_KEY not found in environment variables."}

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = claude_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}]
            )
            text = response.content[0].text
            # Strip markdown code fences if present
            cleaned = re.sub(r'^```json\s*|\s*```$', '', text.strip(), flags=re.MULTILINE)
            return json.loads(cleaned)
        except (anthropic.APIConnectionError, anthropic.RateLimitError) as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning(f"Claude API transient error (attempt {attempt+1}), retrying in {wait}s: {e}")
                time.sleep(wait)
            else:
                return {"error": f"API error after {max_retries} retries: {e}"}
        except anthropic.APIStatusError as e:
            return {"error": f"Claude API status error {e.status_code}: {e.message}"}
        except json.JSONDecodeError as e:
            return {"error": f"Failed to parse Claude response as JSON: {e}"}
        except Exception as e:
            return {"error": f"Unexpected error calling Claude: {e}"}
    return {"error": "API call failed after all retries."}


# =============================================================================
# UTILITY HELPER
# =============================================================================

def count_ema_crossovers(df) -> int:
    """Count how many times ema_fast crossed ema_slow in the given DataFrame window."""
    if 'ema_fast' not in df.columns or 'ema_slow' not in df.columns:
        return 0
    crosses = 0
    for i in range(1, len(df)):
        prev_above = df['ema_fast'].iloc[i - 1] > df['ema_slow'].iloc[i - 1]
        curr_above = df['ema_fast'].iloc[i] > df['ema_slow'].iloc[i]
        if prev_above != curr_above:
            crosses += 1
    return crosses


# =============================================================================
# LIVE BOT: SINGLE GATEKEEPER CALL
# =============================================================================

def get_gatekeeper_decision(
    ticker: str,
    df20,
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
    news_headlines: list,
    setup_name: str = "trend_continuation",
    setup_description: str = None,
) -> dict:
    """
    Single comprehensive Claude call used by the live bot as the final gatekeeper.
    Packages all available context into one prompt for speed (~2-3 sec latency).
    Prompts are shared with local_analyst.py via prompts.py.
    Returns: approved, conviction_score, market_regime, crossover_quality,
             rejection_reason, key_risk, reasoning
    """
    if df20.empty:
        return {"approved": False, "conviction_score": 0, "rejection_reason": "No candle data provided."}

    required_cols = ['open', 'high', 'low', 'close', 'volume', 'ema_fast', 'ema_slow', 'rsi_14', 'adx_14']
    available_cols = [c for c in required_cols if c in df20.columns]
    candle_data_str = df20[available_cols].round(4).to_string()

    # Extract last candle metrics safely
    last = df20.iloc[-1]
    adx_val = last.get('adx_14', 0) if hasattr(last, 'get') else last['adx_14'] if 'adx_14' in df20.columns else 0
    rsi_val = last.get('rsi_14', 50) if hasattr(last, 'get') else last['rsi_14'] if 'rsi_14' in df20.columns else 50

    news_str = "\n".join(f"- {h}" for h in news_headlines) if news_headlines else "No recent news available."

    user_prompt = build_gatekeeper_user_prompt(
        ticker=ticker,
        candle_data_str=candle_data_str,
        adx_val=adx_val,
        rsi_val=rsi_val,
        ema_spread_pct=ema_spread_pct,
        volume_trend=volume_trend,
        crossover_count=crossover_count,
        dist_to_resistance_pct=dist_to_resistance_pct,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        rr_ratio=rr_ratio,
        interval_mins=interval_mins,
        fast_ema=fast_ema,
        slow_ema=slow_ema,
        news_str=news_str,
        setup_name=setup_name,
        setup_description=setup_description,
    )

    result = call_claude_api(GATEKEEPER_SYSTEM_PROMPT, user_prompt, max_tokens=512)

    # Normalise the approved field — Claude may return string "true"/"false"
    if "error" not in result:
        if isinstance(result.get("approved"), str):
            result["approved"] = result["approved"].lower() == "true"
        if "conviction_score" not in result:
            result["conviction_score"] = 0
        if "approved" not in result:
            result["approved"] = False

    return result


# =============================================================================
# TAB 3: 5-AGENT PIPELINE (detailed analysis / debugging)
# =============================================================================

def get_technical_verdict(ticker: str, df_candles) -> dict:
    """
    Deep technical analysis agent for Tab 3 full pipeline.
    Accepts last 20 candles for richer context than the old 5-candle approach.
    """
    if df_candles is None or df_candles.empty:
        return {"error": "No candle data provided."}

    required_cols = ['open', 'high', 'low', 'close', 'volume', 'ema_fast', 'ema_slow', 'rsi_14', 'adx_14']
    missing = [c for c in required_cols if c not in df_candles.columns]
    if missing:
        return {"error": f"Missing columns for technical analysis: {', '.join(missing)}"}

    crossover_count = count_ema_crossovers(df_candles)
    last = df_candles.iloc[-1]
    adx_val = float(last['adx_14']) if 'adx_14' in df_candles.columns else 0
    rsi_val = float(last['rsi_14']) if 'rsi_14' in df_candles.columns else 50
    swing_high = float(df_candles['high'].max())
    current_price = float(last['close'])
    ema_spread_pct = ((float(last['ema_fast']) - float(last['ema_slow'])) / float(last['ema_slow'])) * 100
    dist_to_resistance = ((swing_high - current_price) / current_price) * 100

    avg_vol_recent = df_candles['volume'].tail(3).mean()
    avg_vol_prior = df_candles['volume'].iloc[-6:-3].mean() if len(df_candles) >= 6 else avg_vol_recent
    volume_trend = "increasing" if avg_vol_recent > avg_vol_prior else "decreasing"

    data_str = df_candles[required_cols].round(4).to_string()

    system_prompt = (
        "You are a 'Technical Trader' specialist in an intraday momentum trading system. "
        "You receive recent OHLCV candle data with indicators (EMA, RSI, ADX) and must "
        "determine whether the current Golden Cross setup has genuine edge. "
        "Be SKEPTICAL — only signal BUY if multiple factors independently confirm momentum. "
        "Return ONLY a valid JSON object with no markdown or extra text."
    )

    user_prompt = f"""Perform deep technical analysis for {ticker}.

=== CANDLE DATA (last {len(df_candles)} candles) ===
{data_str}

=== DERIVED METRICS ===
- ADX: {adx_val:.1f} | RSI: {rsi_val:.1f}
- EMA Spread: {ema_spread_pct:.2f}% | Volume Trend: {volume_trend}
- EMA Crossovers in window: {crossover_count} | Distance to 20-bar high: {dist_to_resistance:.1f}%

Analyze:
1. Market regime (trending vs ranging based on ADX + EMA spread)
2. Crossover quality (is this a clean breakout or a whipsaw in choppy price action?)
3. Momentum quality (RSI level + volume trend alignment)
4. Resistance risk (is target blocked by recent swing high?)

Return JSON with exactly these keys:
{{
  "signal": "BUY" or "HOLD",
  "conviction_score": integer 0-100,
  "confidence": "High" or "Medium" or "Low",
  "market_regime": "Trending" or "Ranging" or "Volatile",
  "crossover_quality": "Clean" or "Choppy",
  "rejection_reason": null or one-sentence reason if HOLD,
  "reason": one comprehensive sentence explaining the signal
}}"""

    return call_claude_api(system_prompt, user_prompt)


def get_news_and_sentiment(finnhub_client, ticker: str) -> dict:
    """Fetches Finnhub news and uses Claude to analyze fundamental sentiment."""
    try:
        if not finnhub_client:
            return {"error": "Finnhub client not initialized."}

        today = datetime.now().strftime('%Y-%m-%d')
        one_week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        news = finnhub_client.company_news(ticker, _from=one_week_ago, to=today)

        if not news:
            return {
                "sentiment": "Neutral", "confidence": "Low",
                "summary": "No recent news found for this ticker.",
                "earnings_risk": False, "headline_count": 0
            }

        headlines = [n['headline'] for n in news[:10]]

        system_prompt = (
            "You are a 'Fundamental Analyst' for an intraday trading system. "
            "Analyze news headlines to assess the near-term fundamental outlook for a stock. "
            "Weight recent headlines more heavily than older ones. "
            "Flag earnings risk explicitly — trading into an earnings report is dangerous. "
            "Return ONLY a valid JSON object with no markdown or extra text."
        )

        user_prompt = f"""Analyze the following recent news headlines for {ticker} (most recent first):

{chr(10).join(f'- {h}' for h in headlines)}

Assess the fundamental sentiment and flag any specific risks.

Return JSON with exactly these keys:
{{
  "sentiment": "Bullish" or "Bearish" or "Neutral",
  "confidence": "High" or "Medium" or "Low",
  "summary": one sentence explaining the key driver of sentiment,
  "earnings_risk": true if any headline suggests upcoming earnings or major catalyst, false otherwise,
  "headline_count": integer number of headlines analyzed
}}"""

        result = call_claude_api(system_prompt, user_prompt)
        if "error" not in result:
            result["headline_count"] = result.get("headline_count", len(headlines))
        return result

    except Exception as e:
        return {"error": f"Error fetching or analyzing news: {e}"}


def get_macro_economic_analysis(finnhub_client) -> dict:
    """
    Fetches general market news from Finnhub and uses Claude to assess macro climate.
    Replaces the Gemini Google Search grounding approach.
    """
    try:
        headlines = []
        if finnhub_client:
            try:
                general_news = finnhub_client.general_news(category='general', min_id=0)
                headlines = [n['headline'] for n in general_news[:10] if 'headline' in n]
            except Exception as e:
                logger.warning(f"Could not fetch Finnhub general news: {e}")

        if not headlines:
            return {
                "outlook": "Neutral",
                "risk_factor": "Data unavailable",
                "summary": "No market headlines could be fetched — treating macro as neutral.",
                "confidence": "Low"
            }

        system_prompt = (
            "You are a 'Macroeconomic Analyst' for a US equity trading system. "
            "You receive today's top general financial market headlines. "
            "Assess whether the current macro environment is favorable, neutral, or unfavorable "
            "for intraday momentum trading in US large-cap tech stocks. "
            "Focus on: Fed policy signals, inflation data, recession risk, geopolitical events, "
            "and broad market risk-off indicators. "
            "Return ONLY a valid JSON object with no markdown or extra text."
        )

        user_prompt = f"""Assess the current macro environment for US equity intraday trading based on these headlines:

{chr(10).join(f'- {h}' for h in headlines)}

Return JSON with exactly these keys:
{{
  "outlook": "Positive" or "Negative" or "Neutral",
  "risk_factor": brief name of the biggest risk visible (e.g. "Fed Rate Decision", "Inflation", "Geopolitical"),
  "summary": one sentence describing the overall macro climate for equity trading today,
  "confidence": "High" or "Medium" or "Low"
}}"""

        return call_claude_api(system_prompt, user_prompt)

    except Exception as e:
        return {"error": f"Macro analysis failed: {e}"}


def get_social_sentiment_analysis(finnhub_client, ticker: str) -> dict:
    """
    Uses Finnhub social sentiment data (Reddit + Twitter) and Claude to assess crowd mood.
    Replaces the Gemini Google Search grounding approach.
    """
    try:
        reddit_data = []
        twitter_data = []

        if finnhub_client:
            try:
                today = datetime.now().strftime('%Y-%m-%d')
                week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
                social = finnhub_client.stock_social_sentiment(ticker, _from=week_ago, to=today)
                reddit_data = social.get('reddit', [])
                twitter_data = social.get('twitter', [])
            except Exception as e:
                logger.warning(f"Could not fetch Finnhub social sentiment for {ticker}: {e}")

        if not reddit_data and not twitter_data:
            return {
                "mood": "Quiet",
                "confidence": "Low",
                "summary": f"No social media data available for {ticker} — sentiment unknown."
            }

        # Aggregate the metrics
        reddit_mentions = sum(d.get('mention', 0) for d in reddit_data)
        reddit_pos = sum(d.get('positiveScore', 0) for d in reddit_data)
        reddit_neg = sum(d.get('negativeScore', 0) for d in reddit_data)
        twitter_mentions = sum(d.get('mention', 0) for d in twitter_data)
        twitter_pos = sum(d.get('positiveScore', 0) for d in twitter_data)
        twitter_neg = sum(d.get('negativeScore', 0) for d in twitter_data)

        system_prompt = (
            "You are a 'Social Sentiment Analyst' for an intraday trading system. "
            "You receive quantitative social media data for a stock (Reddit + Twitter/X). "
            "Interpret the mention volume and sentiment scores to gauge crowd mood. "
            "High positive score with rising mentions = Hyped. "
            "High negative score = Fearful. Mixed or low activity = Mixed/Quiet. "
            "Return ONLY a valid JSON object with no markdown or extra text."
        )

        user_prompt = f"""Interpret the following social media data for {ticker} (last 7 days):

Reddit:
- Total mentions: {reddit_mentions}
- Positive score: {reddit_pos:.2f}
- Negative score: {reddit_neg:.2f}

Twitter/X:
- Total mentions: {twitter_mentions}
- Positive score: {twitter_pos:.2f}
- Negative score: {twitter_neg:.2f}

Return JSON with exactly these keys:
{{
  "mood": "Hyped" or "Fearful" or "Mixed" or "Quiet",
  "confidence": "High" or "Medium" or "Low",
  "summary": one sentence interpreting the social activity level and sentiment direction
}}"""

        return call_claude_api(system_prompt, user_prompt)

    except Exception as e:
        return {"error": f"Social sentiment analysis failed: {e}"}


def get_final_decision(ticker: str, tech_report: dict, news_report: dict,
                       macro_report: dict, social_report: dict) -> dict:
    """
    Coordinator agent: synthesizes all 4 specialist reports into a final decision.
    Uses explicit hard veto rules — a single veto overrides all positive signals.
    """
    system_prompt = (
        "You are a 'Hedge Fund Manager' making the final intraday trade decision. "
        "You receive reports from four specialists: Technical, Fundamental, Macroeconomic, Social. "
        "Apply these HARD VETO rules — any single veto means HOLD regardless of other reports:\n"
        "  VETO 1: Technical conviction_score < 70 or signal is not BUY\n"
        "  VETO 2: Fundamental earnings_risk is true\n"
        "  VETO 3: Technical market_regime is 'Ranging'\n"
        "  VETO 4: Macroeconomic outlook is 'Negative'\n"
        "  VETO 5: Fundamental sentiment is 'Bearish' with 'High' confidence\n\n"
        "If no vetoes apply, issue BUY only if at least 3 of 4 specialists are positive/neutral. "
        "Return ONLY a valid JSON object with no markdown or extra text."
    )

    user_prompt = f"""Make the final trade decision for {ticker} based on these specialist reports:

1. TECHNICAL TRADER: {json.dumps(tech_report)}
2. FUNDAMENTAL ANALYST: {json.dumps(news_report)}
3. MACROECONOMIC ANALYST: {json.dumps(macro_report)}
4. SOCIAL SENTIMENT: {json.dumps(social_report)}

First check all 5 hard veto conditions. If any veto fires, the decision is HOLD.
If no vetoes, require 3/4 specialists to be positive/neutral for BUY.

Return JSON with exactly these keys:
{{
  "final_decision": "BUY" or "HOLD",
  "trade_reason": comprehensive sentence citing which agents agreed/disagreed and the decisive factor,
  "veto_applied": true or false,
  "veto_reason": null or the specific veto condition that blocked a BUY
}}"""

    return call_claude_api(system_prompt, user_prompt)
