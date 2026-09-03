"""
position_mgmt.py — ownership-aware open-position management.

Every position carries a source:
  "bot"     — opened by the live loop. The bot manages it fully (trailing
              stop ratchets, leg replacement).
  "ceo"     — opened by an orders.py CEO sheet. DISPLAY-ONLY for the bot:
              it never replaces or cancels these legs. CEO stops move only
              via TIGHTEN_STOP sheets. (Incident 2026-07-15: the bot's 5-min
              ATR trail replaced a CEO swing trade's designed stop.)
  "unknown" — adopted at reconciliation from the broker with no local
              history. Display-only, same as CEO.

The bot still OBSERVES all positions (exit-fill detection, hard_exit_date
enforcement for event_flow, manual override commands) — observation is not
management.
"""

import math
import logging

import pandas_ta as ta

from broker import BrokerError

logger = logging.getLogger(__name__)


def duplicate_entry_exists(broker, ticker: str):
    """DEFENSE IN DEPTH (2026-08-13 duplicate-worker incident): before ANY
    entry, ask the ACCOUNT whether this ticker already has a position or a
    working order. Two workers, a retry, or a stale cache can all produce a
    second bracket; the broker is the only source that sees them all.

    Returns a reason string if an entry must be refused, else None."""
    try:
        for p in broker.get_positions():
            if p.symbol == ticker and abs(float(p.qty)) > 0:
                return f"position already open ({p.qty} sh)"
    except BrokerError as e:
        # Cannot verify -> refuse. A missed trade beats a double fill.
        return f"position check failed: {e}"
    try:
        working = [o for o in broker.get_live_orders(ticker)
                   if o.symbol == ticker]
        if working:
            kinds = ", ".join(sorted({
                str(getattr(o, "order_type", None)
                    or getattr(o, "type", "")).split(".")[-1].lower()
                for o in working}))
            return f"{len(working)} working order(s) already at the broker ({kinds})"
    except BrokerError as e:
        return f"open-order check failed: {e}"
    return None


def is_bot_managed(state: dict) -> bool:
    """Trailing/leg management applies ONLY to positions the bot opened.
    Missing source defaults to 'unknown' -> not managed (safe direction)."""
    return (state or {}).get("source") == "bot"


def compute_trailing_stop(df, risk_profile: dict, current_price: float):
    """New trailing-stop candidate from the profile's ATR/percent rule,
    or None if it can't be computed."""
    trailing_stop_type = risk_profile.get('trailing_stop_type', 'ATR')
    trailing_stop_value = risk_profile.get('trailing_stop_value', 2.0)
    if trailing_stop_type == 'ATR':
        atr_series = ta.atr(df['high'], df['low'], df['close'], length=14)
        if atr_series is None or atr_series.dropna().empty:
            return None
        atr_value = float(atr_series.dropna().iloc[-1])
        if math.isnan(atr_value):
            return None
        return current_price - (atr_value * trailing_stop_value)
    return current_price * (1 - (trailing_stop_value / 100.0))


def r_multiple(state: dict, current_price: float):
    """Open profit in R, measured against the INITIAL structural stop.
    None when the entry/stop geometry is unknown."""
    entry = state.get("entry_price")
    initial = state.get("initial_stop", state.get("trailing_stop_price"))
    try:
        entry, initial = float(entry), float(initial)
    except (TypeError, ValueError):
        return None
    risk = entry - initial
    if risk <= 0:
        return None
    return (current_price - entry) / risk


def reached_one_r(state: dict, current_price: float = None) -> bool:
    """Has this position EVER traded at or above +1R?

    Persisted as `reached_1r`, because r_multiple() reads the CURRENT price:
    without a sticky flag a trade that ran to +1.5R and eased back to +0.9R
    would forget it ever got there, and the breakeven floor would switch off
    at exactly the moment it matters.

    Backfill for positions opened before the flag existed: the ratchet only
    ever ran at r >= 1.0, so a trailing stop that has moved above the initial
    structural stop is itself proof the trade reached +1R."""
    if state.get("reached_1r"):
        return True
    if current_price is not None:
        r = r_multiple(state, current_price)
        if r is not None and r >= 1.0:
            return True
    try:
        initial = float(state.get("initial_stop"))
        trail = float(state.get("trailing_stop_price"))
    except (TypeError, ValueError):
        return False
    return trail > initial


def ratchet_floor(state: dict, candidate: float, current_price: float):
    """The ratified floor (2026-09-03): once a position reaches +1R its stop
    never sits below breakeven again, so `stop = max(ATR trail, entry)`.

    Returns (new_stop, floored: bool). The floor is NOT applied above the
    market — a stop at or above current price would be rejected by the broker
    (or fill instantly), so when price is back under entry the existing stop
    stands and the trade exits on it."""
    if candidate is None:
        return None, False
    if not reached_one_r(state, current_price):
        return candidate, False
    try:
        entry = float(state.get("entry_price"))
    except (TypeError, ValueError):
        return candidate, False
    if entry <= candidate:
        return candidate, False
    if entry >= current_price:
        return candidate, False          # cannot place a stop above market
    return entry, True


def maybe_ratchet_stop(broker, positions: dict, ticker: str, state: dict,
                       df, risk_profile: dict, current_price: float) -> bool:
    """Ratchet a BOT position's broker-side stop leg up. Returns True if the
    stop was replaced. CEO/unknown positions are never touched — that is the
    ownership boundary, enforced here and nowhere else.

    THE INITIAL STOP STANDS UNTIL +1R. The structural stop comes from the
    playbook (reclaim/breakout bar low); trailing it with INTRADAY ATR from
    the moment of entry collapses a 4.9% daily-structure stop to noise
    level — NOK 2026-08-13 entered at 10.76 with a 10.21 stop and was
    trailed out at 10.74 (0.2%) within 42 minutes. ATR trailing is only
    allowed once the trade has paid for its own risk."""
    if not is_bot_managed(state):
        return False

    r = r_multiple(state, current_price)
    at_one_r = reached_one_r(state, current_price)
    if not at_one_r and r is not None and r < 1.0:
        return False        # structural stop stands; not yet +1R
    if at_one_r and not state.get("reached_1r"):
        # Sticky: the trade has paid for its own risk, and that fact must
        # survive a pullback below +1R.
        positions.setdefault(ticker, state)["reached_1r"] = True
        state["reached_1r"] = True

    new_stop = compute_trailing_stop(df, risk_profile, current_price)
    new_stop, floored = ratchet_floor(state, new_stop, current_price)
    if new_stop is None:
        return False
    if not (new_stop > state.get("trailing_stop_price", 0)
            and new_stop < current_price and state.get("stop_order_id")):
        return False
    try:
        new_order = broker.replace_stop(state["stop_order_id"], new_stop)
        positions[ticker]["stop_order_id"] = str(new_order.id)
        positions[ticker]["trailing_stop_price"] = new_stop
        logger.info(f"{ticker}: trailing stop raised to ${new_stop:.2f}"
                    + (" (breakeven floor — at/after +1R)" if floored else ""))
        return True
    except BrokerError as e:
        logger.warning(f"{ticker}: could not replace stop: {e}")
        return False
