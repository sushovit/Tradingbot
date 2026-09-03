"""
Ratchet floor: risk-free at +1R (ratified 2026-09-03). No network.

The rule: once a position reaches +1R, its trailing stop is
max(ATR trail, entry) — it never sits below breakeven again. The CEO-desk
1R rule becomes universal for bot positions.

Found live: CRCL entered 90.21 with a 79.57 structural stop (1R = $10.64),
traded through +1R, and was still trailing at 85.66 — $4.55 BELOW breakeven.
SLB was in the same shape at 55.35 against a 55.74 entry.
"""

import pandas as pd
import pytest

import position_mgmt

PROFILE = {"trailing_stop_type": "ATR", "trailing_stop_value": 2.0,
           "atr_multiplier": 2.0}


def bars(last_close, n=60, step=0.004, spread=0.01):
    """Bars ending near `last_close`. `spread` sets the bar range and so the
    ATR, which is what decides how far below price the trail lands."""
    closes = [last_close - (n - 1 - i) * step for i in range(n)]
    idx = pd.date_range("2026-09-03 09:30", periods=n, freq="5min")
    return pd.DataFrame({"open": closes,
                         "high": [c + spread for c in closes],
                         "low": [c - spread for c in closes], "close": closes,
                         "volume": [100_000] * n}, index=idx)


class FakeOrder:
    def __init__(self, id):
        self.id = id


class FakeBroker:
    def __init__(self):
        self.replaced = []

    def replace_stop(self, order_id, new_stop):
        self.replaced.append((order_id, new_stop))
        return FakeOrder(f"{order_id}-r")


def crcl_state(**over):
    """The live position that exposed the gap."""
    state = {"in_position": True, "source": "bot", "entry_price": 90.21,
             "shares_held": 1, "initial_stop": 79.57,
             "trailing_stop_price": 85.66, "stop_order_id": "stop-crcl",
             "timeframe": "daily"}
    state.update(over)
    return state


# ============================================================ the floor itself

def test_floor_lifts_a_sub_breakeven_trail_to_entry():
    """The CRCL case, exactly."""
    state = crcl_state()
    new_stop, floored = position_mgmt.ratchet_floor(state, 85.66,
                                                    current_price=102.00)
    assert floored is True
    assert new_stop == pytest.approx(90.21)          # entry, not 85.66


def test_floor_never_caps_a_trail_that_is_already_above_entry():
    """max(ATR trail, entry) — the floor is a floor, not a ceiling."""
    state = crcl_state(trailing_stop_price=95.00)
    new_stop, floored = position_mgmt.ratchet_floor(state, 95.00,
                                                    current_price=102.00)
    assert floored is False
    assert new_stop == pytest.approx(95.00)


def test_floor_does_not_apply_before_1r():
    state = crcl_state(trailing_stop_price=79.57)    # never ratcheted
    # +1R is 100.85; at 95.00 the trade has not paid for its risk yet.
    new_stop, floored = position_mgmt.ratchet_floor(state, 88.00,
                                                    current_price=95.00)
    assert floored is False
    assert new_stop == pytest.approx(88.00)


def test_floor_is_never_placed_above_the_market():
    """After +1R the price can fall back under entry. A stop at entry would
    then be above market — rejected by the broker, or an instant fill. The
    existing stop must stand instead."""
    state = crcl_state(reached_1r=True)
    new_stop, floored = position_mgmt.ratchet_floor(state, 85.66,
                                                    current_price=88.00)
    assert floored is False
    assert new_stop == pytest.approx(85.66)


def test_floor_passes_through_a_missing_candidate():
    state = crcl_state(reached_1r=True)
    assert position_mgmt.ratchet_floor(state, None, 102.0) == (None, False)


# ============================================================ stickiness

def test_reaching_1r_is_remembered_after_price_retreats():
    """r_multiple reads the CURRENT price. Without a sticky fact a trade that
    ran to +1.5R and eased to +0.9R would forget it ever got there, and the
    floor would switch off exactly when it matters."""
    state = crcl_state(reached_1r=True, trailing_stop_price=79.57)
    assert position_mgmt.r_multiple(state, 95.00) < 1.0
    assert position_mgmt.reached_one_r(state, 95.00) is True
    new_stop, floored = position_mgmt.ratchet_floor(state, 85.00, 95.00)
    assert floored is True and new_stop == pytest.approx(90.21)


def test_a_moved_trail_backfills_the_flag_for_older_positions():
    """SLB had no flag: it predates it. But the ratchet only ever runs at
    r >= 1.0, so a trail above the INITIAL stop is itself proof."""
    slb = {"in_position": True, "source": "bot", "entry_price": 55.745,
           "initial_stop": 53.12, "trailing_stop_price": 55.346,
           "stop_order_id": "s", "timeframe": "daily"}
    assert position_mgmt.r_multiple(slb, 58.33) < 1.0      # currently +0.98R
    assert position_mgmt.reached_one_r(slb, 58.33) is True
    new_stop, floored = position_mgmt.ratchet_floor(slb, 55.346, 58.33)
    assert floored is True and new_stop == pytest.approx(55.745)


def test_an_untouched_structural_stop_does_not_backfill_the_flag():
    """trail == initial means the ratchet never ran, so nothing is implied."""
    state = crcl_state(trailing_stop_price=79.57)
    assert position_mgmt.reached_one_r(state, 95.00) is False


def test_flag_is_persisted_onto_the_position_when_1r_is_crossed():
    broker = FakeBroker()
    positions = {"CRCL": crcl_state(trailing_stop_price=79.57)}
    position_mgmt.maybe_ratchet_stop(
        broker, positions, "CRCL", positions["CRCL"], bars(102.00),
        PROFILE, current_price=102.00)
    assert positions["CRCL"]["reached_1r"] is True


# ============================================================ end to end

def test_ratchet_raises_the_live_crcl_stop_to_breakeven():
    broker = FakeBroker()
    positions = {"CRCL": crcl_state()}
    changed = position_mgmt.maybe_ratchet_stop(
        broker, positions, "CRCL", positions["CRCL"], bars(102.00),
        PROFILE, current_price=102.00)
    assert changed is True
    _, new_stop = broker.replaced[0]
    assert new_stop >= 90.21                          # never below breakeven
    assert positions["CRCL"]["trailing_stop_price"] >= 90.21


def test_below_1r_the_structural_stop_still_stands():
    """The NOK protection must survive this change untouched."""
    broker = FakeBroker()
    state = {"in_position": True, "source": "bot", "entry_price": 10.76,
             "shares_held": 28, "initial_stop": 10.21,
             "trailing_stop_price": 10.21, "stop_order_id": "stop-1",
             "timeframe": "daily"}
    changed = position_mgmt.maybe_ratchet_stop(
        broker, {"NOK": state}, "NOK", state, bars(10.86), PROFILE,
        current_price=10.86)
    assert changed is False
    assert broker.replaced == []
    assert state["trailing_stop_price"] == 10.21


def test_ceo_positions_are_still_never_touched():
    """The ownership boundary outranks the new rule."""
    broker = FakeBroker()
    state = crcl_state(source="ceo")
    assert position_mgmt.maybe_ratchet_stop(
        broker, {"CRCL": state}, "CRCL", state, bars(102.00), PROFILE,
        current_price=102.00) is False
    assert broker.replaced == []


def test_stop_is_never_moved_down():
    """Monotonic ratchet: a lower candidate is refused even with the floor."""
    broker = FakeBroker()
    positions = {"CRCL": crcl_state(trailing_stop_price=95.00,
                                    reached_1r=True)}
    # Wide bars -> the ATR trail lands well BELOW the existing 95.00 stop,
    # and the floor (entry 90.21) is lower still. Neither may drag it down.
    changed = position_mgmt.maybe_ratchet_stop(
        broker, positions, "CRCL", positions["CRCL"],
        bars(96.00, spread=1.5), PROFILE, current_price=96.00)
    assert changed is False
    assert positions["CRCL"]["trailing_stop_price"] == 95.00


def test_missing_geometry_still_does_not_crash():
    state = {"in_position": True, "source": "bot", "stop_order_id": "s1",
             "trailing_stop_price": 0.0}
    assert position_mgmt.reached_one_r(state, 10.0) is False
    position_mgmt.maybe_ratchet_stop(FakeBroker(), {"X": state}, "X", state,
                                     bars(10.86), PROFILE, 10.86)
