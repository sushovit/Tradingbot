"""
review_bot.py — daily post-close CEO review (Goal 17).

    python review_bot.py

Scheduled ~16:30 ET. Builds the day's bundle (report/universe/floor output,
today's journal rows, open-position status, the intern report if present),
sends ONE API call to the configured review model with a CEO-review system
prompt, posts the review to Discord, and journals it source="review_bot".

READ-ONLY BY CONSTRUCTION: the system prompt states the reviewer cannot
place, modify, or suggest specific orders for auto-execution, and this
module imports no trading path — it never touches orders.py or the broker's
order methods.

Failure mode: API error -> post "review unavailable" to Discord, journal the
error, exit 0. Max 2 retries, no retry-flood.
"""

import json
import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

import clockline          # noqa: E402
import journal            # noqa: E402
import claude_integration  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MAX_RETRIES = 2
DISCORD_MSG_LIMIT = 2000
POSITION_AGE_REVIEW = 10        # sessions
HEARTBEAT_STALE_SECS = 900

REVIEW_SYSTEM_PROMPT = (
    "You are the CEO reviewing a small paper-trading desk's session, post-close. "
    "You receive the day's account report, universe scan, floor status, journal "
    "rows, and the junior analyst's report.\n\n"
    "Your job:\n"
    "1. MARK THE BOOK: state plainly how the account did today and what drove it.\n"
    "2. GRADE THE DAY'S DECISIONS against the playbook rules, citing rule numbers:\n"
    "   Rule 1 = stops come from thesis invalidation, never a fixed percent.\n"
    "   Rule 2 = every signal goes through gatekeeper -> risk gate -> bracket -> journal.\n"
    "   Rule 3 = gap-abort: a reclaim/daily entry is invalid if the session opens "
    "below the signal bar's midpoint.\n"
    "   Also enforced: R:R >= 1.5, notional <= 25% of equity, no margin, max "
    "positions, daily-loss circuit breaker, one intern entry per day.\n"
    "3. FLAG ANOMALIES: stale heartbeat, journal gaps, positions held >= 10 "
    "sessions (re-ratification due), repeated gatekeeper errors, price-freshness "
    "rejections.\n"
    "4. TOMORROW'S WATCH ITEMS: what the desk should watch, as observations.\n\n"
    "HARD CONSTRAINT — YOU ARE READ-ONLY. You cannot place, modify, or cancel "
    "orders, and you must NOT emit specific orders for automatic execution "
    "(no entry/stop/target order sheets). Discuss risk and structure in prose; "
    "any action is a human decision made later.\n\n"
    "Be direct and specific. Cite numbers from the bundle. If the desk did "
    "nothing today, say so and assess whether standing aside was right."
)


def collect_bundle() -> dict:
    """Assemble the day's evidence. Read-only; missing pieces are noted."""
    today = clockline.now_et().strftime("%Y-%m-%d")
    bundle = {"date": today, "clock": clockline.two_zone_line()}

    # Session drop (report + universe + floor), if drop.py has run today.
    drop_path = os.path.join("drop", "latest.md")
    if os.path.exists(drop_path):
        with open(drop_path, "r", encoding="utf-8", errors="replace") as f:
            bundle["drop"] = f.read()[:12000]
    else:
        bundle["drop"] = "(no drop/latest.md — session bundle unavailable)"

    # Intern report.
    intern_path = os.path.join("reports", f"intern_{today}.md")
    if os.path.exists(intern_path):
        with open(intern_path, "r", encoding="utf-8", errors="replace") as f:
            bundle["intern"] = f.read()[:6000]
    else:
        bundle["intern"] = "(no intern report today)"

    # Journal rows.
    try:
        journal.init_db()
        trades = journal.todays_trades()
        bundle["trades"] = trades
        bundle["decision_count"] = journal.decision_count()
        bundle["realized_pnl"] = journal.daily_realized_pnl()
    except Exception as e:
        bundle["trades"] = []
        bundle["journal_error"] = str(e)

    # Heartbeat freshness (anomaly input).
    status_file = "bot_status.log"
    if os.path.exists(status_file):
        age = int(time.time() - os.path.getmtime(status_file))
        bundle["heartbeat_age_secs"] = age
        bundle["heartbeat_stale"] = age > HEARTBEAT_STALE_SECS
    else:
        bundle["heartbeat_age_secs"] = None

    # Open positions + age in sessions (10+ = re-ratification due).
    try:
        from broker import Broker
        broker = Broker()
        positions = []
        for p in broker.get_positions():
            positions.append({
                "ticker": p.symbol, "qty": float(p.qty),
                "entry": float(p.avg_entry_price),
                "current": float(getattr(p, "current_price", 0) or 0),
                "unrealized_pl": float(getattr(p, "unrealized_pl", 0) or 0),
                "sessions_held": position_sessions_held(p.symbol),
            })
        bundle["positions"] = positions
        bundle["equity"] = broker.get_equity()
    except Exception as e:
        bundle["positions"] = []
        bundle["broker_error"] = str(e)
    return bundle


def position_sessions_held(ticker: str):
    """Trading sessions since the journaled BUY (None if unknown)."""
    try:
        buy = journal.last_buy_for_ticker(ticker)
        if not buy:
            return None
        import pandas as pd
        opened = pd.Timestamp(buy["timestamp"][:10])
        return int(len(pd.bdate_range(opened, clockline.now_et().date())) - 1)
    except Exception:
        return None


def build_user_prompt(bundle: dict) -> str:
    pos_lines = "\n".join(
        f"- {p['ticker']}: {p['qty']:g} @ ${p['entry']:,.2f} (now ${p['current']:,.2f}, "
        f"unrealized ${p['unrealized_pl']:+,.2f}, held {p['sessions_held']} sessions"
        + (" — REVIEW DUE" if (p["sessions_held"] or 0) >= POSITION_AGE_REVIEW else "")
        + ")"
        for p in bundle.get("positions", [])) or "- none"
    trade_lines = "\n".join(
        f"- {t['timestamp']} {t['action']} {t['qty']:g} {t['ticker']} @ "
        f"${t['price']:,.2f} (PnL ${t['pnl_usd']:+,.2f}) [{t['reason']}]"
        for t in bundle.get("trades", [])) or "- none"
    hb = bundle.get("heartbeat_age_secs")
    return f"""SESSION REVIEW — {bundle['date']}
{bundle['clock']}

=== ACCOUNT ===
Equity: ${bundle.get('equity', 0):,.2f} | Realized PnL today: ${bundle.get('realized_pnl', 0):+,.2f}
Decisions journaled today: {bundle.get('decision_count', 0)}
Worker heartbeat age: {hb if hb is not None else 'unknown'}s{' (STALE)' if bundle.get('heartbeat_stale') else ''}

=== OPEN POSITIONS ===
{pos_lines}

=== TODAY'S FILLS ===
{trade_lines}

=== SESSION BUNDLE (report / universe / floor) ===
{bundle.get('drop', '')}

=== JUNIOR ANALYST REPORT ===
{bundle.get('intern', '')}

Write the review: mark the book, grade the decisions against the rules
(cite rule numbers), flag anomalies, list tomorrow's watch items.
Remember: you are read-only — no orders, no order sheets."""


def request_review(bundle: dict) -> dict:
    """One API call (max 2 retries) with prompt caching on the static system
    prompt. Returns {'text': ...} or {'error': ...}."""
    client = claude_integration._get_client()
    if client is None:
        return {"error": "ANTHROPIC_API_KEY not configured"}
    model = claude_integration.get_model("review")
    user_prompt = build_user_prompt(bundle)

    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=2000,
                system=[{"type": "text", "text": REVIEW_SYSTEM_PROMPT,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user_prompt}],
            )
            return {"text": claude_integration.extract_text(resp),
                    "model": model}
        except Exception as e:
            last_err = str(e)
            message = last_err.lower()
            fallback = claude_integration.get_fallback_model()
            if "model" in message and model != fallback:
                print(f"MODEL NOT FOUND: '{model}' - falling back to '{fallback}'")
                model = fallback
                continue
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
    return {"error": last_err or "unknown error"}


def post_discord(content: str):
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        print("(no webhook configured)")
        return
    try:
        from discord_webhook import DiscordWebhook
        body = content if len(content) <= DISCORD_MSG_LIMIT else \
            content[:DISCORD_MSG_LIMIT - 40] + "\n... (truncated)"
        resp = DiscordWebhook(url=url, content=body).execute()
        print(f"Posted to Discord (HTTP {getattr(resp, 'status_code', '?')})")
    except Exception as e:
        print(f"(Discord post failed: {e})")


def main() -> int:
    bundle = collect_bundle()
    result = request_review(bundle)
    journal.init_db()

    if "error" in result:
        journal.log_decision(
            "DESK", "daily_review", {"date": bundle["date"]},
            {"approved": False, "error": result["error"],
             "rejection_reason": "review_unavailable"},
            source="review_bot")
        post_discord(f"⚠️ Daily review unavailable ({bundle['date']}): "
                     f"{result['error'][:300]}")
        print(f"Review unavailable: {result['error']}")
        return 0                      # never crash the scheduled job

    review = result["text"]
    journal.log_decision(
        "DESK", "daily_review",
        {"date": bundle["date"], "positions": len(bundle.get("positions", [])),
         "trades": len(bundle.get("trades", [])), "model": result.get("model")},
        {"approved": True, "rejection_reason": None, "reasoning": review[:4000]},
        source="review_bot")
    header = f"📋 Daily review — {bundle['date']}\n"
    post_discord(header + review)
    print(header + review)
    return 0


if __name__ == "__main__":
    sys.exit(main())
