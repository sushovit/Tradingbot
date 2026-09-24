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

REVIEW_SYSTEM_PROMPT_TEMPLATE = (
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
    "  (d) LOCAL SHADOW ANALYST: {shadow_fact} Shadow is ADVISORY and "
    "non-blocking by CEO ruling. Raise it only if a shadow APPROVAL "
    "appears, or the error rate moves materially from the figure above.\n"
    "  (e) A WORKER RESTART is an ops artifact, not a decision. It resets "
    "the cycle counter, so cycle numbers starting again at #1 mid-session "
    "mean a restart, not a new session. It also empties in-memory caches, "
    "which before 2026-09-20 could make the gatekeeper re-ask about a "
    "signal bar it had already declined (SPCX 09-17: 68, restart, 78, "
    "bought). Flag restart artifacts as ops events; do NOT grade a re-ask "
    "as a second decision, and do not read a duplicated verdict as the "
    "gatekeeper changing its mind.\n"
    "  (f) SOFT VOLUME (S8, live 2026-09-21): a mean_reversion_reclaim "
    "signal whose reclaim bar traded between 1.0x and the configured "
    "multiplier of its 20-bar average volume is no longer refused in "
    "code. It is sent to the gatekeeper FLAGGED soft_volume, with the "
    "shortfall stated, for the gatekeeper to weigh. A soft-volume "
    "signal in the decision log is the design working. Do NOT report "
    "it as a filter failure or as the volume rule being skipped, and "
    "do NOT read 'reached the gatekeeper' as 'approved by the "
    "rules' - the deterministic filters passing is the START of the "
    "decision, never the end of it. Below 1.0x is still refused in "
    "code and never reaches the gatekeeper at all.\n"
    "  (g) mean_reversion_reclaim IS ON PROBATION (S7, from "
    "2026-09-21): 20 live trades, ONE concurrent position, counted "
    "from prompt_version v5 ONLY. The setup has earlier live history, "
    "but under a gatekeeper prompt that graded it with "
    "trend-continuation rules, and those entries are deliberately not "
    "counted toward the new gate. A probation count of 0/20 sitting "
    "beside older reclaim trades in the ledger is correct - not a "
    "reset and not a lost record. Grade reclaim trades in the "
    "PROBATION section alongside pullback_in_uptrend and "
    "post_earnings_continuation, not as an established setup.\n\n"
    "HARD CONSTRAINT — YOU ARE READ-ONLY. You cannot place, modify, or cancel "
    "orders, and you must NOT emit specific orders for automatic execution "
    "(no entry/stop/target order sheets). Discuss risk and structure in prose; "
    "any action is a human decision made later.\n\n"
    "Be direct and specific. Cite numbers from the bundle. If the desk did "
    "nothing today, say so and assess whether standing aside was right."
)


# --- Generation budget (2026-09-24) ------------------------------------------
# Three memos truncated in a row: 09-21 zero chars, 09-22 twenty-four, 09-23
# 584, each cut mid-sentence. The signature of a thinking-capable model
# spending the whole output budget before it starts writing.
#
# max_tokens is the WHOLE output allowance - thinking and answer share it.
# At 8000 a long reasoning pass left nothing for the memo. 16000 gives the
# answer real room; `effort` is what bounds the thinking half.
#
# NOT budget_tokens. Sonnet 5 REMOVED that parameter - sending
# {"type": "enabled", "budget_tokens": N} returns a 400 and would fail every
# review outright. `output_config.effort` is the supported control: it caps
# how deep the model reasons, which is the same lever by a different name.
REVIEW_MAX_TOKENS = 16000
REVIEW_EFFORT = "medium"          # low | medium | high | xhigh | max

# The memo has five numbered sections (see the duties in the system prompt).
# If section 5 is missing, generation stopped early whatever the API said.
REQUIRED_SECTION = "## 5."


def memo_is_complete(text: str) -> bool:
    """Does this text contain the last section the memo is required to have?

    A length check cannot tell a short flat-day memo from a truncated one.
    The final section header can: it is the last thing written, so its
    presence means the model reached the end."""
    return REQUIRED_SECTION in (text or "")


def call_diagnostics(resp, max_tokens: int, text: str = "") -> dict:
    """Everything needed to tell truncation from refusal from an empty
    answer. None of this was recorded for the three lost memos, which is why
    the cause took three days to find."""
    usage = getattr(resp, "usage", None)
    return {
        "stop_reason": getattr(resp, "stop_reason", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "input_tokens": getattr(usage, "input_tokens", None),
        "max_tokens": max_tokens,
        "effort": REVIEW_EFFORT,
        "thinking": "adaptive",
        "text_chars": len(text or ""),
        "has_section_5": memo_is_complete(text),
    }


def write_failure_stub(date: str, clock: str, diagnostics: dict) -> str:
    """Write review_<date>.md saying the generation failed, and return the
    line that was written.

    A missing file reads as "the job has not run yet". A one-line file that
    names the failure reads as "the job ran and failed", which is the true
    state and the one an operator can act on. The three truncated memos were
    worse than either: they looked like complete reviews of quiet days."""
    diagnostics = diagnostics or {}
    line = (f"GENERATION FAILED: {diagnostics.get('stop_reason')}, "
            f"{diagnostics.get('output_tokens')} output tokens")
    try:
        os.makedirs("reports", exist_ok=True)
        path = os.path.join("reports", f"review_{date}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# Daily review — {date}\n{clock}\n\n{line}\n")
        print(f"Failure stub written: {path}")
    except OSError as e:
        print(f"(could not write failure stub: {e})")
    return line


def newest_drop_for(date: str):
    """The last session drop written for an ET date, or None.

    drop/latest.md is a moving pointer, so a backfill has to name the file
    by date or it would review today's session under yesterday's heading."""
    try:
        names = sorted(n for n in os.listdir("drop")
                       if n.startswith(f"session_ET{date}_")
                       and n.endswith(".md"))
    except OSError:
        return None
    return os.path.join("drop", names[-1]) if names else None


def collect_bundle(date: str = None) -> dict:
    """Assemble the day's evidence. Read-only; missing pieces are noted.

    `date` re-runs a PAST session (YYYY-MM-DD). The journal rows and the
    drop are read for that date; the broker positions are not, because the
    broker only reports the book as it stands now. A backfilled bundle says
    so, rather than letting the reviewer read today's marks as history."""
    today = date or clockline.now_et().strftime("%Y-%m-%d")
    bundle = {"date": today, "clock": clockline.two_zone_line()}
    if date:
        bundle["backfill"] = (
            f"BACKFILL: this review was generated later, for the session of "
            f"{date}. Journal rows, the drop and the intern report are that "
            f"session's. The OPEN POSITIONS and their marks are CURRENT, not "
            f"as of {date} - do not read them as that day's closing state, "
            f"and do not reconcile them against that day's numbers.")

    # Session drop (report + universe + floor), if drop.py has run today.
    drop_path = (newest_drop_for(date) if date
                 else os.path.join("drop", "latest.md"))
    if drop_path is None:
        drop_path = os.path.join("drop", "__missing__")
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
        trades = journal.todays_trades(today)
        bundle["trades"] = trades
        bundle["decision_count"] = journal.decision_count(today)
        bundle["realized_pnl"] = journal.daily_realized_pnl(today)
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


def shadow_fact() -> str:
    """The live shadow figures, computed at prompt-build time.

    A hardcoded snapshot drifts and then gets argued about: on 2026-09-20
    the desk believed the baseline was ~38% while the journal held 10.4%
    over 316 decisions. Deriving it removes the argument."""
    try:
        s = journal.shadow_error_rate()
        if not s["total"]:
            return "no shadow decisions journaled yet."
        return (f"it has approved {s['approved']} of {s['total']} journaled "
                f"decisions, with a {s['rate_pct']}% error rate whose cause "
                f"is known (the service was not running at session start).")
    except Exception:
        return ("its approval count and error rate are known and tracked in "
                "the journal.")


def review_system_prompt() -> str:
    return REVIEW_SYSTEM_PROMPT_TEMPLATE.replace("{shadow_fact}",
                                                 shadow_fact())


# Kept as a module attribute so existing callers and tests keep working.
REVIEW_SYSTEM_PROMPT = review_system_prompt()


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
    backfill = bundle.get("backfill")
    return f"""SESSION REVIEW — {bundle['date']}
{bundle['clock']}
{(chr(10) + "*** " + backfill + chr(10)) if backfill else ""}
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

    max_tokens = REVIEW_MAX_TOKENS
    last_err = None
    last_diag = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                # Thinking and answer share max_tokens. `effort` bounds the
                # thinking half; budget_tokens is a 400 on Sonnet 5.
                thinking={"type": "adaptive"},
                output_config={"effort": REVIEW_EFFORT},
                system=[{"type": "text", "text": REVIEW_SYSTEM_PROMPT,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user_prompt}],
            )
            text = claude_integration.extract_text(resp)
            diag = call_diagnostics(resp, max_tokens, text)
            print(f"review call: model={model} " +
                  " ".join(f"{k}={v}" for k, v in diag.items()))

            if text.strip() and (diag["stop_reason"] != "end_turn"
                                 or not memo_is_complete(text)):
                # TRUNCATION. A memo that stopped early is worse than none:
                # it reads like a complete review of a quiet day. Both tests
                # are needed - end_turn alone passed a memo that stopped
                # after section 2, and the section check alone would accept
                # a max_tokens cut that happened to get that far.
                last_err = (f"incomplete review "
                            f"(stop_reason={diag['stop_reason']}, "
                            f"{diag['output_tokens']} output tokens, "
                            f"{diag['text_chars']} chars, "
                            f"section_5={diag['has_section_5']})")
                print(f"INCOMPLETE REVIEW on attempt {attempt + 1}: "
                      f"{last_err}")
                last_diag = diag
                if attempt < MAX_RETRIES:
                    # Double the budget rather than repeat the same request:
                    # a retry at the ceiling that just truncated will mostly
                    # truncate again.
                    max_tokens *= 2
                    print(f"retrying with max_tokens={max_tokens}")
                    time.sleep(2 ** attempt)
                    continue
                return {"error": last_err, "diagnostics": diag}

            if not text.strip():
                # 2026-09-21: reports/review_2026-09-21.md was written 129
                # bytes long - the header and the clock line and nothing
                # else - because the response carried no text block at all
                # and "" formatted into the memo without complaint. An
                # EMPTY memo is a failure, not a memo, and it has to travel
                # the error path so the retry fires and, if it still fails,
                # the operator is told. Silence that looks like success is
                # the worst of both.
                stop = getattr(resp, "stop_reason", None)
                kinds = [getattr(b, "type", "?")
                         for b in (getattr(resp, "content", None) or [])]
                usage = getattr(resp, "usage", None)
                last_err = (f"empty review text (stop_reason={stop}, "
                            f"content blocks={kinds or 'none'}, "
                            f"output_tokens="
                            f"{getattr(usage, 'output_tokens', '?')})")
                print(f"EMPTY REVIEW on attempt {attempt + 1}: {last_err}")
                last_diag = diag
                if attempt < MAX_RETRIES:
                    max_tokens *= 2
                    time.sleep(2 ** attempt)
                    continue
                return {"error": last_err, "diagnostics": diag}
            return {"text": text, "model": model}
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
    return {"error": last_err or "unknown error", "diagnostics": last_diag}


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


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    backfill_date = None
    if "--date" in argv:
        i = argv.index("--date")
        if i + 1 < len(argv):
            backfill_date = argv[i + 1]

    bundle = collect_bundle(date=backfill_date)
    result = request_review(bundle)
    journal.init_db()

    if "error" in result:
        journal.log_decision(
            "DESK", "daily_review", {"date": bundle["date"]},
            {"approved": False, "error": result["error"],
             "rejection_reason": "review_unavailable"},
            source="review_bot")
        line = write_failure_stub(bundle["date"], bundle.get("clock", ""),
                                  result.get("diagnostics"))
        post_discord(f"⚠️ Daily review FAILED ({bundle['date']}): {line}\n"
                     f"{result['error'][:300]}")
        print(f"Review unavailable: {result['error']}")
        return 0                      # never crash the scheduled job

    review = result["text"]
    if not review.strip() or not memo_is_complete(review):
        # Unreachable via request_review, which now fails on empty text.
        # Kept because THIS is the line that creates the file: a future
        # caller that skips request_review must not be able to leave a
        # header-only memo on disk for drop.py to carry to the CEO desk.
        journal.log_decision(
            "DESK", "daily_review", {"date": bundle["date"]},
            {"approved": False,
             "error": "empty or incomplete review text",
             "rejection_reason": "review_unavailable"},
            source="review_bot")
        line = write_failure_stub(
            bundle["date"], bundle.get("clock", ""),
            {"stop_reason": "incomplete", "output_tokens": len(review)})
        post_discord(f"⚠️ Daily review FAILED ({bundle['date']}): {line}")
        print(f"Review empty or incomplete - {line}")
        return 0

    # Persist the memo so drop.py can carry it to the CEO desk (Discord is
    # delivery, not storage).
    try:
        os.makedirs("reports", exist_ok=True)
        memo_path = os.path.join("reports", f"review_{bundle['date']}.md")
        with open(memo_path, "w", encoding="utf-8") as f:
            f.write(f"# Daily review — {bundle['date']}\n"
                    f"{bundle.get('clock', '')}\n\n{review}\n")
        print(f"Memo written: {memo_path} ({len(review)} chars)")
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
