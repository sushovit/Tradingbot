"""
risk.py — the ONE set of risk rules every trade goes through.

Used by both the live bot loop (strategy signals) and orders.py (CEO order
sheets). No entry path may bypass these checks.
"""

import math

MIN_REWARD_RISK = 1.5
# Per-position notional cap fallback when bot_config.json lacks
# "max_position_pct" — policy lives in CONFIG, this is only the safe default.
DEFAULT_MAX_POSITION_PCT = 0.30


def max_position_pct(config: dict) -> float:
    """Per-position notional cap as a FRACTION of equity, from
    bot_config.json "max_position_pct" (e.g. 0.25 = 25%). Falls back to
    0.30 for old configs or invalid values."""
    try:
        value = float((config or {}).get("max_position_pct",
                                         DEFAULT_MAX_POSITION_PCT))
    except (TypeError, ValueError):
        return DEFAULT_MAX_POSITION_PCT
    if not (0.0 < value <= 1.0):
        return DEFAULT_MAX_POSITION_PCT
    return value


def effective_equity(broker_equity: float, config: dict) -> float:
    """The ONLY equity figure any sizing/validation path may use.

    A hard code-level capital cap (bot_config.json "capital_cap_usd")
    independent of the broker balance: a $97k paper account with a $1,000 cap
    trades like a $1,000 account. No path may size off raw broker equity."""
    cap = (config or {}).get("capital_cap_usd")
    if cap is None:
        return float(broker_equity)
    return min(float(broker_equity), float(cap))


def reward_risk(entry: float, stop: float, target: float):
    """R:R multiple, or None if the geometry is invalid (stop not below entry)."""
    risk = entry - stop
    if risk <= 0:
        return None
    return (target - entry) / risk


def check_signal(entry: float, stop, target, equity: float,
                 notional_usd: float = None,
                 open_positions: int = 0, max_positions: int = 3,
                 daily_pnl: float = 0.0, daily_loss_limit_usd: float = None,
                 open_notional_usd: float = 0.0,
                 position_cap_pct: float = None):
    """Validate one prospective long entry. Returns (ok: bool, reason: str|None).

    Rules:
      - a stop is mandatory (thesis invalidation must be defined)
      - reward:risk must be >= 1.5
      - notional must not exceed position_cap_pct of equity (config
        "max_position_pct"; default 30%)
      - NO MARGIN: open notional + new notional must not exceed equity (cash)
      - must not exceed max simultaneous positions
      - the daily-loss circuit breaker must not be tripped

    `equity` must already be the EFFECTIVE equity (see effective_equity)."""
    cap_pct = position_cap_pct if position_cap_pct else DEFAULT_MAX_POSITION_PCT
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
        if notional_usd > equity * cap_pct:
            return False, "notional_exceeds_max_position_pct"
        # Never use margin buying power: total deployed cash <= equity.
        if open_notional_usd + notional_usd > equity:
            return False, "insufficient_cash_no_margin"

    if open_positions >= max_positions:
        return False, "max_positions_reached"

    if daily_loss_limit_usd is not None and daily_pnl <= -abs(daily_loss_limit_usd):
        return False, "circuit_breaker"

    return True, None


def position_size(equity: float, risk_per_trade_pct: float,
                  entry: float, stop: float,
                  open_notional_usd: float = 0.0,
                  position_cap_pct: float = None) -> int:
    """Risk-based sizing in WHOLE shares: (entry - stop) * shares equals the
    per-trade dollar risk budget. Returns 0 if geometry is invalid.

    Notional is capped at position_cap_pct of equity (config
    "max_position_pct") AND at remaining cash (no margin).
    `equity` must already be the effective equity."""
    cap_pct = position_cap_pct if position_cap_pct else DEFAULT_MAX_POSITION_PCT
    risk_per_share = entry - stop
    if risk_per_share <= 0 or equity <= 0:
        return 0
    dollar_risk = equity * (risk_per_trade_pct / 100.0)
    shares = math.floor(dollar_risk / risk_per_share)
    max_notional = min(equity * cap_pct,
                       max(equity - open_notional_usd, 0.0))
    if shares * entry > max_notional:
        shares = math.floor(max_notional / entry)
    return max(shares, 0)


# --- B-book: experimental half-risk slot (Goal 19) -------------------------
# Tier A is the real book. Tier B is a sandbox: half risk, one open position,
# one new entry per calendar week — journaled separately so A-book statistics
# stay pure.
TIER_B_RISK_PCT = 0.5
# Boardroom #2 (2026-09-01): two concurrent tier-B slots, was one. Half risk
# and the weekly cadence apply PER SLOT, so the experiment can run two
# independent probes without doubling the risk of either.
TIER_B_MAX_OPEN = 2
TIER_B_ENTRIES_PER_WEEK = 2


def tier_risk_pct(tier: str, default_pct: float) -> float:
    return TIER_B_RISK_PCT if str(tier).upper() == "B" else default_pct


def check_tier_b(open_b_positions: int, b_entries_this_week: int):
    """B-book gates. Returns (ok, reason)."""
    if open_b_positions >= TIER_B_MAX_OPEN:
        return False, "b_book_position_open"
    if b_entries_this_week >= TIER_B_ENTRIES_PER_WEEK:
        return False, "b_book_weekly_limit"
    return True, None



# --- Setup probation (revised 2026-09-02, owner-ratified) -----------------
# ORIGINAL RULE: half risk for the first 20 trades. It was withdrawn because
# it interacted badly with the $2,000 capital cap — a 0.5% budget is $10, so
# any setup with a stop wider than $10/share sized to zero. Half the live
# dry-run signals were unsizeable, which would have biased probation toward
# tight-stop trades: a biased sample, slowly gathered.
#
# REVISED RULE: full 1% risk, but only ONE concurrent open position per
# probation setup. Exposure is bounded by CONCURRENCY rather than by size, so
# every qualifying signal is sizeable and the 20-trade sample is unbiased.
PROBATION_TRADES = 20
PROBATION_MAX_CONCURRENT = 1
PROBATION_SETUPS = ("pullback_in_uptrend", "post_earnings_continuation")


def probation_setups(config: dict = None) -> tuple:
    cfg = ((config or {}).get("setup_probation") or {})
    return tuple(cfg.get("setups") or PROBATION_SETUPS)


def probation_limit(config: dict = None) -> int:
    cfg = ((config or {}).get("setup_probation") or {})
    try:
        return int(cfg.get("trades", PROBATION_TRADES))
    except (TypeError, ValueError):
        return PROBATION_TRADES


def probation_max_concurrent(config: dict = None) -> int:
    cfg = ((config or {}).get("setup_probation") or {})
    try:
        return int(cfg.get("max_concurrent", PROBATION_MAX_CONCURRENT))
    except (TypeError, ValueError):
        return PROBATION_MAX_CONCURRENT


def on_probation(setup_name: str, live_trades: int,
                 config: dict = None) -> bool:
    """Is this setup still serving probation?"""
    if setup_name not in probation_setups(config):
        return False
    try:
        return int(live_trades) < probation_limit(config)
    except (TypeError, ValueError):
        return True          # unknown count -> stay cautious


def setup_risk_pct(setup_name: str, live_trades: int, default_pct: float,
                   config: dict = None) -> float:
    """Risk budget for one entry.

    Probation no longer discounts size (see the note above): a probation
    trade is a full-size trade. Kept as the single call site so the sizing
    path does not have to change again if the rule changes again."""
    return default_pct


def check_setup_probation(setup_name: str, open_for_setup: int,
                          live_trades: int, config: dict = None):
    """Concurrency gate for a probation setup. Returns (ok, reason).

    This is what bounds probation exposure now. One open position per
    probation setup: a setup that turns out to be wrong can be wrong once at
    a time, not three times simultaneously."""
    if not on_probation(setup_name, live_trades, config):
        return True, None
    try:
        open_n = int(open_for_setup)
    except (TypeError, ValueError):
        open_n = 0
    if open_n >= probation_max_concurrent(config):
        return False, "probation_position_open"
    return True, None


def zero_size_reason(entry: float, equity: float,
                     position_cap_pct: float = None) -> str:
    """Why did sizing produce < 1 whole share? Journaled as a rules pass so we
    learn which tickers this account cannot afford (Alpaca brackets need
    whole shares)."""
    cap_pct = position_cap_pct if position_cap_pct else DEFAULT_MAX_POSITION_PCT
    if entry > equity * cap_pct:
        return "price_too_high_for_account"
    return "size_zero"


def daily_loss_limit_usd(equity: float, daily_loss_limit_pct: float) -> float:
    return equity * (abs(daily_loss_limit_pct) / 100.0)
