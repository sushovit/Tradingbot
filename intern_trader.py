"""
intern_trader.py — the ONLY module allowed to trade the intern account.

Isolation contract (Goal 12b):
  - Every broker handle constructed here is Broker(account="intern") —
    INTERN_ALPACA_* keys only. Main-account keys are never read here.
  - Main-desk code (orders.py, streamlit_app.py) never constructs an
    intern broker; the bot's position management runs against the main
    account and cannot see intern positions at all.
  - intern_desk.py (the read-only analysis desk) imports this module ONLY
    inside its trade/override commands, keeping the analysis path clean.

Capital structure mirrors the main desk: cap $2,000, 25% per position,
1% risk per trade, max 2 positions, 3% daily loss (measured broker-side as
equity vs last_equity, so it needs no journal schema change).

Every decision and rejection is journaled source="intern"; CEO overrides
source="ceo_override". One new entry per day maximum.
"""

import os
import json
import time
import logging
from datetime import datetime

import pytz

import journal
import risk
from broker import Broker, BrokerError

logger = logging.getLogger(__name__)

EASTERN_TZ = pytz.timezone("US/Eastern")
INTERN_POSITIONS_FILE = "intern_positions.json"
INTERN_TRADE_PREFIX = "INTERN"          # trades-table reason prefix

INTERN_CONFIG = {"capital_cap_usd": 2000, "max_position_pct": 0.25}
RISK_PER_TRADE_PCT = 1.0
MAX_POSITIONS = 2
DAILY_LOSS_LIMIT_PCT = 3.0
MIN_CONVICTION = 70
TARGET_R = 2.0


def get_intern_broker() -> Broker:
    return Broker(account="intern")


def _load_state() -> dict:
    try:
        with open(INTERN_POSITIONS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict):
    with open(INTERN_POSITIONS_FILE, "w") as f:
        json.dump(state, f, indent=4)


def _today_et() -> str:
    return datetime.now(EASTERN_TZ).strftime("%Y-%m-%d")


def is_trading_day(dt=None) -> bool:
    """Weekend guard: no entries when the market is closed (weekday check;
    exchange holidays still fall through to the broker's rejection)."""
    dt = dt or datetime.now(EASTERN_TZ)
    return dt.weekday() < 5


def _journal_intern_rejection(ticker: str, setup_name, reason: str,
                              context: dict) -> None:
    journal.log_decision(
        ticker, setup_name or "intern_trade",
        context,
        {"approved": False, "rejection_reason": reason, "source": "intern"},
        source="intern")


def select_candidate(verdicts: dict):
    """His TOP-conviction long_setup with conviction >= MIN_CONVICTION,
    or None (a valid, gradeable outcome)."""
    longs = [(t, v) for t, v in verdicts.items()
             if v.get("stance") == "long_setup"
             and isinstance(v.get("conviction"), (int, float))
             and v["conviction"] >= MIN_CONVICTION]
    if not longs:
        return None
    return max(longs, key=lambda kv: kv[1]["conviction"])


def already_traded_today() -> bool:
    for t in journal.todays_trades():
        if t["action"] == "BUY" and str(t.get("reason", "")).startswith(INTERN_TRADE_PREFIX):
            return True
    return False


def _intern_daily_pnl(broker) -> float:
    """Broker-side daily PnL: equity vs last_equity (prior session close)."""
    acct = broker.get_account()
    try:
        return float(acct.equity) - float(acct.last_equity)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def execute_trade(verdicts: dict, broker: Broker = None) -> str:
    """The intern's one allowed entry per day. Returns a report line."""
    journal.init_db()

    # Weekend runs are analysis-only: --trade must no-op cleanly, journaled.
    if not is_trading_day():
        journal.log_decision(
            "NONE", "intern_trade", {"date": _today_et()},
            {"approved": False, "rejection_reason": "market_closed",
             "reasoning": "Non-trading day — analysis only, no entry attempted.",
             "source": "intern"},
            source="intern")
        return "Trade: skipped — market closed (non-trading day)."

    if already_traded_today():
        return "Trade: skipped — already entered a position today (1/day max)."

    picked = select_candidate(verdicts)
    if picked is None:
        journal.log_decision(
            "NONE", "intern_trade", {"date": _today_et()},
            {"approved": False, "rejection_reason": "no_trade_today",
             "reasoning": f"No long_setup with conviction >= {MIN_CONVICTION}.",
             "source": "intern"},
        source="intern")
        return (f"Trade: **no trade today** — no long_setup at conviction "
                f">= {MIN_CONVICTION}. (Valid, gradeable outcome.)")

    ticker, verdict = picked
    setup = verdict.get("setup_name")
    conviction = verdict["conviction"]

    # A tradeable idea without a stop is itself a gradeable failure.
    stop = verdict.get("invalidation")
    if not isinstance(stop, (int, float)) or isinstance(stop, bool):
        _journal_intern_rejection(ticker, setup, "no_stop_in_verdict",
                                  {"conviction": conviction,
                                   "reasoning": verdict.get("reasoning", "")})
        return (f"Trade: REJECTED {ticker} (conviction {conviction}) — "
                f"**no numeric invalidation in his verdict**. Journaled as a "
                f"gradeable failure (no_stop_in_verdict).")

    if broker is None:
        broker = get_intern_broker()

    try:
        equity = risk.effective_equity(broker.get_equity(), INTERN_CONFIG)
        open_positions = broker.get_positions()
        entry = broker.get_latest_price(ticker)
    except (BrokerError, Exception) as e:
        return f"Trade: aborted — intern broker unreachable: {e}"

    stop = float(stop)
    open_count = len(open_positions)
    open_notional = sum(abs(float(p.qty)) * float(p.avg_entry_price)
                        for p in open_positions)
    daily_pnl = _intern_daily_pnl(broker)
    loss_limit = risk.daily_loss_limit_usd(equity, DAILY_LOSS_LIMIT_PCT)
    target = entry + (entry - stop) * TARGET_R          # 2R from entry vs stop

    context = {"entry": entry, "stop": stop, "target": target,
               "conviction": conviction, "equity": equity,
               "reasoning": verdict.get("reasoning", "")}

    if stop >= entry:
        _journal_intern_rejection(ticker, setup, "stop_not_below_entry", context)
        return (f"Trade: REJECTED {ticker} — his invalidation ${stop:,.2f} is at/above "
                f"current price ${entry:,.2f} (stop_not_below_entry).")

    qty = risk.position_size(equity, RISK_PER_TRADE_PCT, entry, stop,
                             open_notional_usd=open_notional,
                             position_cap_pct=INTERN_CONFIG["max_position_pct"])
    if qty < 1:
        reason = risk.zero_size_reason(entry, equity,
                                       position_cap_pct=INTERN_CONFIG["max_position_pct"])
        _journal_intern_rejection(ticker, setup, reason, context)
        return f"Trade: REJECTED {ticker} — sizing produced <1 share ({reason})."

    ok, reason = risk.check_signal(
        entry, stop, target, equity, notional_usd=qty * entry,
        open_positions=open_count, max_positions=MAX_POSITIONS,
        daily_pnl=daily_pnl, daily_loss_limit_usd=loss_limit,
        open_notional_usd=open_notional,
        position_cap_pct=INTERN_CONFIG["max_position_pct"])
    if not ok:
        _journal_intern_rejection(ticker, setup, reason, context)
        return f"Trade: REJECTED {ticker} by the risk gate ({reason})."

    decision_id = journal.log_decision(
        ticker, setup or "intern_trade", context,
        {"approved": True, "conviction_score": conviction,
         "stance": "long_setup", "setup_name": setup,
         "invalidation": stop, "rejection_reason": None,
         "key_risk": verdict.get("key_risk", ""),
         "reasoning": verdict.get("reasoning", "")},
        source="intern")

    try:
        bracket = broker.submit_bracket(ticker, qty, stop, target)
    except BrokerError as e:
        _journal_intern_rejection(ticker, setup, f"order_failed:{e}", context)
        return f"Trade: ORDER FAILED for {ticker}: {e}"

    fill_price = entry
    try:
        for _ in range(8):
            o = broker.get_order(bracket.id)
            status = str(getattr(o, "status", "")).lower().split(".")[-1]
            if status == "filled" and getattr(o, "filled_avg_price", None):
                fill_price = float(o.filled_avg_price)
                break
            time.sleep(1)
    except BrokerError:
        pass

    trade_id = journal.log_trade(
        ticker, "BUY", qty, fill_price,
        reason=f"{INTERN_TRADE_PREFIX} {setup or 'long_setup'}",
        decision_id=decision_id, broker_order_id=str(bracket.id))

    state = _load_state()
    state[ticker] = {"in_position": True, "entry_price": fill_price,
                     "shares_held": qty, "stop": stop, "target": target,
                     "decision_id": decision_id, "entry_trade_id": trade_id,
                     "opened": _today_et()}
    _save_state(state)

    return (f"Trade: ✅ BOUGHT {qty} {ticker} @ ~${fill_price:,.2f} on the INTERN "
            f"account — stop ${stop:,.2f} (his invalidation), target ${target:,.2f} "
            f"(2R), conviction {conviction}.")


def close_own_positions(verdicts: dict, broker: Broker = None) -> list:
    """The desk may close its OWN positions: if today's verdict on a held
    ticker is no longer long_setup, exit with his reasoning (journaled)."""
    journal.init_db()
    state = _load_state()
    held = {t: s for t, s in state.items() if s.get("in_position")}
    if not held:
        return []
    if broker is None:
        broker = get_intern_broker()

    lines = []
    for ticker, pos in held.items():
        verdict = verdicts.get(ticker)
        if verdict is None or verdict.get("stance") == "long_setup":
            continue
        reasoning = (verdict.get("reasoning", "")
                     or f"stance changed to {verdict.get('stance')}")
        try:
            close_order = broker.close_position(ticker)
            price = broker.get_latest_price(ticker)
        except BrokerError as e:
            lines.append(f"Close: FAILED {ticker}: {e}")
            continue
        journal.log_decision(
            ticker, "intern_close", {"held_since": pos.get("opened")},
            {"approved": True, "action": "close",
             "stance_today": verdict.get("stance"),
             "reasoning": reasoning, "rejection_reason": None},
            source="intern")
        journal.record_exit(
            ticker, pos.get("shares_held", 0), price,
            f"{INTERN_TRADE_PREFIX} close: {reasoning[:120]}",
            decision_id=pos.get("decision_id"),
            broker_order_id=str(getattr(close_order, "id", "")) or None,
            entry_price=pos.get("entry_price"))
        state[ticker] = {"in_position": False}
        lines.append(f"Close: {ticker} exited — {reasoning[:120]}")
    _save_state(state)
    return lines


def override_close(ticker: str, reason: str) -> int:
    """CEO override: close an intern position, journaled source=ceo_override."""
    journal.init_db()
    ticker = ticker.upper()
    broker = get_intern_broker()
    try:
        close_order = broker.close_position(ticker)
        price = broker.get_latest_price(ticker)
    except BrokerError as e:
        print(f"Override-close FAILED for {ticker}: {e}")
        return 1

    state = _load_state()
    pos = state.get(ticker, {})
    journal.log_decision(
        ticker, "ceo_override", {"reason": reason},
        {"approved": True, "action": "override_close",
         "reasoning": reason, "rejection_reason": None},
        source="ceo_override")
    journal.record_exit(
        ticker, pos.get("shares_held", 0), price,
        f"CEO override-close: {reason[:120]}",
        decision_id=pos.get("decision_id"),
        broker_order_id=str(getattr(close_order, "id", "")) or None,
        entry_price=pos.get("entry_price"))
    state[ticker] = {"in_position": False}
    _save_state(state)
    print(f"Override-closed {ticker} @ ~${price:,.2f} — journaled (ceo_override).")
    return 0
