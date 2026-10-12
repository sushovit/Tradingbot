"""
owner_close.py — S18. The owner closes a bot position by hand, on the record.

    venv/bin/python orders.py close <TICKER>

close_bot_position():
  1. refuses unless <TICKER> is an OPEN BOT position in positions.json and
     is held at the broker;
  2. cancels the position's bracket legs and confirms none is still live
     (the broker rejects a sell while the legs hold the shares);
  3. market-sells the FULL broker quantity and waits for the fill;
  4. journals the exit as "Owner close" at the real fill price and fill
     time (journal.finalize_owner_close also covers the case where a running
     worker journaled the same fill first), records what the rule held at
     that moment (stop, target, profit lock) for the report, and marks the
     position closed in positions.json.

rule_outcome() prices the counterfactual for the report: the position's
own stop, target and profit lock replayed on 5-minute bars from the close to
the next 16:00 ET session close, valued at that close if neither leg fills.
"""

import json
import math
import time
from datetime import datetime

import pandas as pd
import pytz

import journal
import position_mgmt
import safe_io

ET = pytz.timezone("America/New_York")
POSITIONS_FILE = "positions.json"
CONFIG_FILE = "bot_config.json"
CANCEL_WAIT_SECS = 30
FILL_WAIT_SECS = 60


class OwnerCloseRefused(Exception):
    """The close was not attempted (or stopped before selling)."""


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def _profit_lock_for(ticker, state, config):
    profile_name = (config.get("ticker_profiles") or {}).get(ticker, "Moderate")
    profile = (config.get("risk_profiles") or {}).get(profile_name) or {}
    if position_mgmt.trailing_type_for(state, profile) != position_mgmt.PROFIT_LOCK:
        return []
    return [list(step) for step in position_mgmt.profit_lock_steps(profile)]


def close_bot_position(ticker, broker, positions_file=POSITIONS_FILE,
                       config_file=CONFIG_FILE, sleep=time.sleep, out=print):
    ticker = ticker.upper()
    positions = _load_json(positions_file, {})
    state = positions.get(ticker) or {}
    if not state.get("in_position"):
        raise OwnerCloseRefused(f"{ticker} is not an open position in "
                                f"{positions_file} - nothing to close.")
    if state.get("source") != "bot":
        raise OwnerCloseRefused(
            f"{ticker} is not a bot position (source "
            f"{state.get('source') or 'unknown'}) - refusing.")
    held = {p.symbol: p for p in broker.get_positions()}
    if ticker not in held:
        raise OwnerCloseRefused(f"{ticker} is not held at the broker - "
                                f"refusing (run orders.py sync).")
    qty = abs(float(held[ticker].qty))

    for order in broker.get_live_orders(ticker):
        broker.cancel_order(str(order.id))
    for _ in range(CANCEL_WAIT_SECS):
        if not broker.get_live_orders(ticker):
            break
        sleep(1)
    else:
        raise OwnerCloseRefused(f"{ticker}: bracket legs still live after "
                                f"{CANCEL_WAIT_SECS}s - NOT selling.")
    out(f"{ticker}: bracket legs cancelled; selling {qty:g} at market.")

    order = broker.close_position(ticker)
    fill_price = filled_at = None
    for _ in range(FILL_WAIT_SECS):
        o = broker.get_order(order.id)
        if str(getattr(o, "status", "")).lower().endswith("filled") \
                and getattr(o, "filled_avg_price", None):
            fill_price = float(o.filled_avg_price)
            filled_at = pd.Timestamp(o.filled_at)
            break
        sleep(1)
    if fill_price is None:
        raise OwnerCloseRefused(
            f"{ticker}: close order {order.id} submitted but not filled within "
            f"{FILL_WAIT_SECS}s - run `orders.py sync` once it fills.")
    if filled_at.tzinfo is None:
        filled_at = filled_at.tz_localize("UTC")
    filled_at_et = filled_at.tz_convert(ET).strftime("%Y-%m-%d %H:%M:%S")

    order_id = str(order.id)
    entry = state.get("entry_price")
    journal.record_exit(ticker, qty, fill_price, journal.OWNER_CLOSE_REASON,
                        decision_id=state.get("decision_id"),
                        broker_order_id=order_id, entry_price=entry,
                        sector=state.get("sector"))
    trade_id = journal.finalize_owner_close(order_id, filled_at_et)
    config = _load_json(config_file, {})
    journal.record_owner_close_context(
        trade_id, order_id, ticker, qty, entry, fill_price, filled_at_et,
        stop=state.get("trailing_stop_price"),
        target=state.get("profit_target_price"),
        original_stop=position_mgmt.original_stop(state),
        profit_lock=_profit_lock_for(ticker, state, config))

    positions[ticker] = {"in_position": False}
    safe_io.atomic_write_text(positions_file, json.dumps(positions, indent=4))
    pnl = (fill_price - entry) * qty if entry else None
    out(f"{ticker}: SOLD {qty:g} @ {fill_price:.4f} at {filled_at_et} ET"
        + (f" | P&L ${pnl:+.2f}" if pnl is not None else "")
        + f" | journaled as '{journal.OWNER_CLOSE_REASON}' (trade {trade_id})")
    return {"ticker": ticker, "qty": qty, "fill_price": fill_price,
            "filled_at": filled_at_et, "trade_id": trade_id, "pnl": pnl,
            "order_id": order_id}


# ============================================================ counterfactual

def next_session_close(filled_at_et: pd.Timestamp) -> pd.Timestamp:
    """The first 16:00 ET at or after the fill on a weekday."""
    ts = filled_at_et
    close = ts.normalize() + pd.Timedelta(hours=16)
    if ts >= close:
        close += pd.Timedelta(days=1)
    while close.weekday() >= 5:
        close += pd.Timedelta(days=1)
    return close


def rule_outcome(bars, filled_at_et, entry, stop, target, original_stop,
                 profit_lock):
    """(exit_price, reason) had the rule kept the position until the next
    session close, or (None, "pending") if that close is not in the bars yet.

    5-minute bars stamped with their START time. Fills as everywhere else: a
    bar opening through a level fills at its open; a bar spanning both hits
    the stop first. The profit lock steps on each completed close, applies
    from the next bar, never lowers."""
    until = next_session_close(filled_at_et)
    after = bars[(bars.index >= filled_at_et) & (bars.index < until)]
    if after.empty or bars.index[-1] < until - pd.Timedelta(minutes=5):
        return None, "pending"
    steps = tuple(tuple(s) for s in (profit_lock or ()))
    live_stop = stop if stop is not None else -math.inf
    target = target if target is not None else math.inf
    state = {"entry_price": entry, "original_stop": original_stop}
    first = True
    for _, bar in after.iterrows():
        o, h, l, c = (float(bar["open"]), float(bar["high"]),
                      float(bar["low"]), float(bar["close"]))
        if not first and o <= live_stop:
            return o, "stop (gap)"
        if not first and o >= target:
            return o, "target (gap)"
        first = False
        if l <= live_stop:
            return live_stop, "stop"
        if h >= target:
            return target, "target"
        if steps:
            raised, _ = position_mgmt.profit_lock_target(state, c, steps)
            if raised is not None and live_stop < raised < c:
                live_stop = raised
    return float(after["close"].iloc[-1]), "held to the session close"
