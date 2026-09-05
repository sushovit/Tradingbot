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
    "4. GRADE EVERY PROBATION TRADE listed under PROBATION TRADES. Each of "
    "a new setup's first 20 live trades gets its OWN line, in the form "
    "'<setup> #<n> <TICKER>: <good|bad|ungradeable> - <one sentence>'. "
    "Grade the DECISION against the setup's thesis, not the outcome: a "
    "trade that lost money on a correct read is 'good', and a winner that "
    "ignored the thesis is 'bad'. Say 'ungradeable' only when the "
    "evidence is genuinely absent. If the list is empty, write "
    "'PROBATION TRADES: none yet'.\n"
    "5. TOMORROW'S WATCH ITEMS: what the desk should watch, as observations.\n\n"
    "DESK FACTS THE REVIEWER MUST NOT RE-FLAG. These are settled. Do "
    "not raise them as anomalies, rule violations or open questions; "
    "mention one only if it CHANGES.\n"
    "  (a) BREAKEVEN FLOOR (ratified 2026-09-03): a bot-managed position "
    "at or after +1R carries stop = max(ATR trail, entry price). A stop "
    "sitting exactly at the entry price is that rule working, NOT a "
    "Rule 1 violation and not a fixed-percent stop. When you report how "
    "far a stop sits from price, measure it from ENTRY, not from the "
    "current price — a risk-free position has zero risk regardless of "
    "how far price has run.\n"
    "  (b) SESSION SHUTDOWN: the worker shuts itself down at "
    "session_end_et (16:15 ET) and releases its lock. A heartbeat around "
    "900 s old at 16:30 ET is the EXPECTED post-shutdown state, not a "
    "stall. Only a stale heartbeat DURING the session is an anomaly.\n"
    "  (c) NOK and ORCL, 2026-08-13: the duplicate-looking closed-trade "
    "rows are REAL double fills from the duplicate-worker incident, not "
    "journal duplication — each leg carries a distinct broker order id "
    "(NOK 28 sh twice, ORCL 1 sh twice). Cumulative PnL is correct as "
    "recorded. The single-instance lock fixed the cause.\n"
    "  (d) LOCAL SHADOW ANALYST: as of 2026-09-05 it has approved 0 of "
    "~275 journaled decisions, with a ~11% error rate whose cause is "
    "known and fixed (the service was not running at session start). "
    "Shadow is ADVISORY and non-blocking by CEO ruling. Raise it only if "
    "a shadow APPROVAL appears, or the error rate moves materially.\n\n"
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
            drop_text = f.read()
        bundle["drop"] = drop_text[:12000]
        # PROVENANCE: the embedded bundle may cover a DIFFERENT session than
        # "today" (a post-close review at 16:30 ET vs a drop written that
        # morning, or a manual run after midnight). Without this the two
        # counters look like a contradiction — the reviewer flagged exactly
        # that. State the drop's own stamp so it can reconcile them.
        stamp = ""
        for line in drop_text.splitlines()[:6]:
            if " ET  |  " in line:
                stamp = line.strip()
                break
        bundle["drop_stamp"] = stamp or "(no header stamp found)"
        bundle["drop_date"] = stamp[:10] if stamp else "unknown"
    else:
        bundle["drop"] = "(no drop/latest.md — session bundle unavailable)"
        bundle["drop_stamp"] = "(none)"
        bundle["drop_date"] = "unknown"

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

    # Governance: CEO rulings, overrides, tags — the desk being RUN.
    try:
        bundle["governance"] = journal.governance_rows()
        bundle["counts"] = journal.decision_counts()
    except Exception as e:
        bundle["governance"] = []
        bundle["counts"] = {"error": str(e)}

    # Open positions + LIVE bracket geometry so R:R is verifiable, plus the
    # risk-free flag that kills the BAC REVIEW-DUE false positive at source.
    try:
        from broker import Broker
        broker = Broker()
        live_stops, live_targets = {}, {}
        try:
            for o in broker.get_live_orders():
                otype = str(getattr(o, "order_type", None)
                            or getattr(o, "type", "")).lower()
                if "stop" in otype and getattr(o, "stop_price", None):
                    live_stops[o.symbol] = float(o.stop_price)
                elif "limit" in otype and getattr(o, "limit_price", None):
                    live_targets[o.symbol] = float(o.limit_price)
        except Exception as e:
            bundle["orders_error"] = str(e)

        positions = []
        for p in broker.get_positions():
            entry = float(p.avg_entry_price)
            current = float(getattr(p, "current_price", 0) or 0)
            stop = live_stops.get(p.symbol)
            target = live_targets.get(p.symbol)
            risk_amt = (current - stop) if (stop and current) else None
            reward_amt = (target - current) if (target and current) else None
            positions.append({
                "ticker": p.symbol, "qty": float(p.qty),
                "entry": entry, "current": current,
                "unrealized_pl": float(getattr(p, "unrealized_pl", 0) or 0),
                "sessions_held": position_sessions_held(p.symbol),
                "stop": stop, "target": target,
                "stop_pct": round((current - stop) / current * 100, 2)
                if (stop and current) else None,
                "target_pct": round((target - current) / current * 100, 2)
                if (target and current) else None,
                "rr_remaining": round(reward_amt / risk_amt, 2)
                if (risk_amt and risk_amt > 0 and reward_amt is not None) else None,
                # Stop at/above entry: nothing left to re-underwrite.
                "risk_free": bool(stop is not None and stop >= entry),
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


def probation_lines() -> str:
    """One line per probation trade for the reviewer to grade.

    Every one of a new setup's first 20 live trades gets an explicit graded
    line (owner ruling 2026-09-02). Listing them HERE, rather than hoping the
    reviewer notices them among the day's fills, is what makes the grade
    mandatory and auditable."""
    try:
        import json as _json
        import risk as _risk
        with open("bot_config.json", encoding="utf-8") as f:
            cfg = _json.load(f)
        limit = _risk.probation_limit(cfg)
        out = []
        for name in _risk.probation_setups(cfg):
            for r in journal.probation_trades(name, limit):
                if r["closed"]:
                    result = (f"closed {r['pnl_usd']:+.2f} USD "
                              f"({r['pnl_pct']:+.2f}%) via {r['exit_reason']}")
                else:
                    result = "still OPEN"
                out.append(f"{name} #{r['n']}/{limit} {r['date']} "
                           f"{r['ticker']} {r['qty']}sh @ ${r['entry']:.2f} "
                           f"- {result}")
        return chr(10).join(out) if out else "(none yet)"
    except Exception as e:
        return f"(probation trade list unavailable: {e})"


def build_user_prompt(bundle: dict) -> str:
    pos_lines = "\n".join(
        f"- {p['ticker']}: {p['qty']:g} @ ${p['entry']:,.2f} (now ${p['current']:,.2f}, "
        f"unrealized ${p['unrealized_pl']:+,.2f}, held {p['sessions_held']} sessions) | "
        f"stop {('$%.2f (-%.2f%%)' % (p['stop'], p['stop_pct'])) if p.get('stop') else 'NONE'}"
        f" | target {('$%.2f (+%.2f%%)' % (p['target'], p['target_pct'])) if p.get('target') else 'NONE'}"
        f" | R:R remaining {p.get('rr_remaining') if p.get('rr_remaining') is not None else 'n/a'}"
        + (" | RISK-FREE (stop at/above entry — exempt from re-ratification)"
           if p.get("risk_free") else "")
        + (" | REVIEW DUE"
           if (p["sessions_held"] or 0) >= POSITION_AGE_REVIEW
           and not p.get("risk_free") else "")
        for p in bundle.get("positions", [])) or "- none"
    probation_lines_text = probation_lines()
    gov_lines = "\n".join(
        f"- {g['time']} [{g['source']}] {g['ticker']} {g['setup'] or ''}: "
        + ("APPROVED" if g["approved"] else "REJECTED")
        + (f" tag={g['tag']}" if g.get("tag") else "")
        + (f" reason={g['reason']}" if g.get("reason") else "")
        + (f" — {g['note']}" if g.get("note") else "")
        for g in bundle.get("governance", [])) or "- none"
    counts = bundle.get("counts", {})
    trade_lines = "\n".join(
        f"- {t['timestamp']} {t['action']} {t['qty']:g} {t['ticker']} @ "
        f"${t['price']:,.2f} (PnL ${t['pnl_usd']:+,.2f}) [{t['reason']}]"
        for t in bundle.get("trades", [])) or "- none"
    hb = bundle.get("heartbeat_age_secs")
    return f"""SESSION REVIEW — {bundle['date']}
{bundle['clock']}

=== ACCOUNT ===
Equity: ${bundle.get('equity', 0):,.2f} | Realized PnL today: ${bundle.get('realized_pnl', 0):+,.2f}
Decisions journaled today: {counts.get('total', bundle.get('decision_count', 0))} total
  = {counts.get('gatekeeper_calls', 0)} gatekeeper/CEO calls
  + {counts.get('rules_passes', 0)} deterministic rules passes
  + the remainder (intern desk, review). Full breakdown: {counts.get('by_source', {})}
  NOTE: "gatekeeper calls" and "decisions" are DIFFERENT counters — they
  are not expected to match.
Worker heartbeat age: {hb if hb is not None else 'unknown'}s{' (STALE)' if bundle.get('heartbeat_stale') else ''}

=== OPEN POSITIONS (live bracket geometry — R:R is verifiable from this) ===
{pos_lines}

=== GOVERNANCE DECISIONS TODAY (CEO rulings, overrides, tags) ===
{gov_lines}

=== TODAY'S FILLS ===
{trade_lines}

=== PROBATION TRADES (grade each one; owner-ratified 2026-09-02) ===
{probation_lines_text}

=== SESSION BUNDLE (report / universe / floor) ===
PROVENANCE: this bundle was generated {bundle.get('drop_stamp', 'unknown')}
(session date {bundle.get('drop_date', 'unknown')}). The account/journal
figures above are for {bundle['date']}. If those dates differ, the two sets
of counts describe DIFFERENT sessions and are not in conflict — say which
session each number belongs to rather than reporting a mismatch.

{bundle.get('drop', '')}

=== JUNIOR ANALYST REPORT ===
{bundle.get('intern', '')}

Write the review: mark the book, grade the decisions against the rules
(cite rule numbers), GRADE EVERY PROBATION TRADE on its own line, flag
anomalies, list tomorrow's watch items.
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
                # Sonnet 5 spends part of this budget on thinking tokens —
                # the first live memo (2026-07-27) was cut mid-sentence at
                # 1,229 chars with max_tokens=2000. Give the memo real room.
                max_tokens=8000,
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


def post_discord(content: str, attach_full: str = None, date: str = None):
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        print("(no webhook configured)")
        return
    try:
        from discord_webhook import DiscordWebhook
        long_memo = len(content) > DISCORD_MSG_LIMIT
        body = content if not long_memo else \
            content[:DISCORD_MSG_LIMIT - 60] + "\n... (full memo attached)"
        wh = DiscordWebhook(url=url, content=body)
        if long_memo and attach_full:
            wh.add_file(file=attach_full.encode("utf-8"),
                        filename=f"review_{date or 'memo'}.md")
        resp = wh.execute()
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
    # Persist the memo so drop.py can carry it to the CEO desk (Discord is
    # delivery, not storage).
    try:
        os.makedirs("reports", exist_ok=True)
        memo_path = os.path.join("reports", f"review_{bundle['date']}.md")
        with open(memo_path, "w", encoding="utf-8") as f:
            f.write(f"# Daily review — {bundle['date']}\n"
                    f"{bundle.get('clock', '')}\n\n{review}\n")
        print(f"Memo written: {memo_path}")
    except OSError as e:
        print(f"(could not write memo file: {e})")

    journal.log_decision(
        "DESK", "daily_review",
        {"date": bundle["date"], "positions": len(bundle.get("positions", [])),
         "trades": len(bundle.get("trades", [])), "model": result.get("model")},
        {"approved": True, "rejection_reason": None, "reasoning": review[:4000]},
        source="review_bot")
    header = f"📋 Daily review — {bundle['date']}\n"
    # Long memos exceed Discord's 2,000-char limit: post the head inline and
    # attach the full text so nothing is lost.
    post_discord(header + review, attach_full=review, date=bundle["date"])
    print(header + review)
    return 0


if __name__ == "__main__":
    sys.exit(main())
