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


def maybe_ratchet_stop(broker, positions: dict, ticker: str, state: dict,
                       df, risk_profile: dict, current_price: float) -> bool:
    """Ratchet a BOT position's broker-side stop leg up. Returns True if the
    stop was replaced. CEO/unknown positions are never touched — that is the
    ownership boundary, enforced here and nowhere else."""
    if not is_bot_managed(state):
        return False

    new_stop = compute_trailing_stop(df, risk_profile, current_price)
    if new_stop is None:
        return False
    if not (new_stop > state.get("trailing_stop_price", 0)
            and new_stop < current_price and state.get("stop_order_id")):
        return False
    try:
        new_order = broker.replace_stop(state["stop_order_id"], new_stop)
        positions[ticker]["stop_order_id"] = str(new_order.id)
        positions[ticker]["trailing_stop_price"] = new_stop
        logger.info(f"{ticker}: trailing stop raised to ${new_stop:.2f}")
        return True
    except BrokerError as e:
        logger.warning(f"{ticker}: could not replace stop: {e}")
        return False
