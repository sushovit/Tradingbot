"""
risk.py — the ONE set of risk rules every trade goes through.

Used by both the live bot loop (strategy signals) and orders.py (CEO order
sheets). No entry path may bypass these checks.
"""

import math

MIN_REWARD_RISK = 1.5
MAX_NOTIONAL_PCT_OF_EQUITY = 30.0


def reward_risk(entry: float, stop: float, target: float):
    """R:R multiple, or None if the geometry is invalid (stop not below entry)."""
    risk = entry - stop
    if risk <= 0:
        return None
    return (target - entry) / risk


def check_signal(entry: float, stop, target, equity: float,
                 notional_usd: float = None,
                 open_positions: int = 0, max_positions: int = 3,
                 daily_pnl: float = 0.0, daily_loss_limit_usd: float = None):
    """Validate one prospective long entry. Returns (ok: bool, reason: str|None).

    Rules:
      - a stop is mandatory (thesis invalidation must be defined)
      - reward:risk must be >= 1.5
      - notional must not exceed 30% of equity
      - must not exceed max simultaneous positions
      - the daily-loss circuit breaker must not be tripped
    """
    if stop is None or (isinstance(stop, float) and math.isnan(stop)):
        return False, "missing_stop"
    if target is None or (isinstance(target, float) and math.isnan(target)):
        return False, "missing_target"
    if entry is None or entry <= 0:
        return False, "invalid_entry"
    if stop >= entry:
        return False, "stop_not_below_entry"

    rr = reward_risk(entry, stop, target)
    if rr is None or rr < MIN_REWARD_RISK:
        return False, f"reward_risk_below_{MIN_REWARD_RISK}"

    if notional_usd is not None and equity > 0:
        if notional_usd > equity * (MAX_NOTIONAL_PCT_OF_EQUITY / 100.0):
            return False, "notional_exceeds_30pct_equity"

    if open_positions >= max_positions:
        return False, "max_positions_reached"

    if daily_loss_limit_usd is not None and daily_pnl <= -abs(daily_loss_limit_usd):
        return False, "circuit_breaker"

    return True, None


def position_size(equity: float, risk_per_trade_pct: float,
                  entry: float, stop: float) -> int:
    """Risk-based sizing: shares such that (entry - stop) * shares equals the
    per-trade dollar risk budget. Returns 0 if geometry is invalid."""
    risk_per_share = entry - stop
    if risk_per_share <= 0 or equity <= 0:
        return 0
    dollar_risk = equity * (risk_per_trade_pct / 100.0)
    shares = math.floor(dollar_risk / risk_per_share)
    # Never let the position notional exceed the 30%-of-equity cap.
    max_notional = equity * (MAX_NOTIONAL_PCT_OF_EQUITY / 100.0)
    if shares * entry > max_notional:
        shares = math.floor(max_notional / entry)
    return max(shares, 0)


def daily_loss_limit_usd(equity: float, daily_loss_limit_pct: float) -> float:
    return equity * (abs(daily_loss_limit_pct) / 100.0)
