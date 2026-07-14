"""
Risk-rule validation for orders.py — no broker, no network.
validate_order() is a pure function; nothing here touches Alpaca.
"""

from datetime import datetime, timedelta

import pytz

from orders import validate_order, validate_sheet

EASTERN_TZ = pytz.timezone("US/Eastern")
NOW = EASTERN_TZ.localize(datetime(2026, 7, 3, 10, 0, 0))

EQUITY = 1000.0
MAX_POSITIONS = 3


def buy_order(**overrides):
    order = {
        "action": "BUY",
        "ticker": "NVDA",
        "notional_usd": 250,
        "entry": 100.0,
        "stop": 95.0,
        "target": 110.0,   # R:R = 2.0
        "setup": "momentum_continuation",
        "reason": "textbook breakout",
    }
    order.update(overrides)
    return order


def test_valid_order_accepted():
    ok, reason = validate_order(buy_order(), EQUITY, 0, MAX_POSITIONS, NOW)
    assert ok, f"expected acceptance, got: {reason}"


def test_missing_stop_rejected():
    order = buy_order()
    del order["stop"]
    ok, reason = validate_order(order, EQUITY, 0, MAX_POSITIONS, NOW)
    assert not ok
    assert reason == "missing_stop"


def test_bad_reward_risk_rejected():
    # entry 100, stop 95, target 104 -> R:R = 0.8 < 1.5
    ok, reason = validate_order(buy_order(target=104.0), EQUITY, 0, MAX_POSITIONS, NOW)
    assert not ok
    assert "reward_risk" in reason


def test_oversized_notional_rejected():
    # 40% of equity > default 30% cap
    ok, reason = validate_order(buy_order(notional_usd=400), EQUITY, 0, MAX_POSITIONS, NOW)
    assert not ok
    assert reason == "notional_exceeds_max_position_pct"


def test_max_positions_rejected():
    ok, reason = validate_order(buy_order(), EQUITY, 3, MAX_POSITIONS, NOW)
    assert not ok
    assert reason == "max_positions_reached"


def test_stop_above_entry_rejected():
    ok, reason = validate_order(buy_order(stop=101.0), EQUITY, 0, MAX_POSITIONS, NOW)
    assert not ok
    assert reason == "stop_not_below_entry"


def test_event_flow_requires_hard_exit_date():
    order = buy_order(setup="event_flow")
    ok, reason = validate_order(order, EQUITY, 0, MAX_POSITIONS, NOW)
    assert not ok
    assert reason == "event_flow_missing_hard_exit_date"

    order["hard_exit_date"] = "2026-07-10"
    ok, reason = validate_order(order, EQUITY, 0, MAX_POSITIONS, NOW)
    assert ok, f"expected acceptance, got: {reason}"


def test_expired_order_rejected():
    expired = (NOW - timedelta(hours=1)).isoformat()
    ok, reason = validate_order(buy_order(valid_until=expired),
                                EQUITY, 0, MAX_POSITIONS, NOW)
    assert not ok
    assert reason == "order_expired"


def test_invalid_action_rejected():
    ok, reason = validate_order(buy_order(action="YOLO"), EQUITY, 0, MAX_POSITIONS, NOW)
    assert not ok
    assert reason.startswith("invalid_action")


def test_exits_always_allowed_even_at_max_positions():
    for action in ("SELL", "CLOSE", "TAKE_PARTIAL"):
        ok, _ = validate_order(buy_order(action=action), EQUITY, 3, MAX_POSITIONS, NOW)
        assert ok


# =============================================================================
# Config-driven per-position notional cap (max_position_pct)
# =============================================================================

def test_max_position_pct_read_from_config():
    import risk
    assert risk.max_position_pct({"max_position_pct": 0.25}) == 0.25
    # Garbage falls back to the safe default, never crashes.
    assert risk.max_position_pct({"max_position_pct": "huge"}) == 0.30
    assert risk.max_position_pct({"max_position_pct": 5.0}) == 0.30


def test_pct25_cap_on_2000_rejects_501_accepts_499():
    # New policy: 25% of $2,000 = $500 per position.
    order_501 = buy_order(notional_usd=501, entry=100.0, stop=95.0, target=110.0)
    ok, reason = validate_order(order_501, 2000.0, 0, MAX_POSITIONS, NOW,
                                position_cap_pct=0.25)
    assert not ok
    assert reason == "notional_exceeds_max_position_pct"

    order_499 = buy_order(notional_usd=499, entry=100.0, stop=95.0, target=110.0)
    ok, reason = validate_order(order_499, 2000.0, 0, MAX_POSITIONS, NOW,
                                position_cap_pct=0.25)
    assert ok, f"expected acceptance, got: {reason}"


def test_default_30pct_when_key_missing():
    import risk
    assert risk.max_position_pct({}) == 0.30
    # $610 > 30% of $2,000 ($600) -> rejected; $590 accepted.
    ok, reason = validate_order(buy_order(notional_usd=610, entry=100.0),
                                2000.0, 0, MAX_POSITIONS, NOW)
    assert not ok and reason == "notional_exceeds_max_position_pct"
    ok, reason = validate_order(buy_order(notional_usd=590, entry=100.0),
                                2000.0, 0, MAX_POSITIONS, NOW)
    assert ok, f"expected acceptance, got: {reason}"


def test_sheet_schema_validation():
    assert validate_sheet({"session": "s", "regime": "r", "orders": []}) == []
    errors = validate_sheet({"orders": "not-a-list"})
    assert any("orders" in e for e in errors)
    assert any("session" in e for e in errors)
