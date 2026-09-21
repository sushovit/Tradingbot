"""
S6 (agenda 10.6): daily positions do not trail. No network.

THE RULE. `trailing_stop_type: "none"` means a position keeps the stop and
target it was opened with, for the life of the trade. Profiles select it for
daily positions via `daily_trailing: "none"`; intraday positions are
untouched and keep the ATR trail plus the +1R breakeven floor.

WHY THE SPLIT. A daily stop is a THESIS level — the reclaim or breakout bar
low. Moving it on price alone converts a 4R plan into a 1R scalp, which is
the same failure as NOK (2026-08-13, a daily structure trailed on 5-minute
ATR), one level up: right timeframe, wrong idea. Intraday positions have no
such structural level to defend, so nothing changes for them.

COMPATIBILITY. `daily_trailing` falls back to `trailing_stop_type`, so a
profile that never mentions it keeps the behaviour it already had. The key
can only ever be an explicit choice — which is what test_ratchet_floor.py
and test_daily_trail_guard.py rely on to keep testing the floor.
"""

import json

import pandas as pd
import pytest

import position_mgmt


CONFIG = json.load(open("bot_config.json", encoding="utf-8"))

# A profile shaped like the live ones: daily positions do not trail.
PROFILE = {"trailing_stop_type": "ATR", "trailing_stop_value": 2.0,
           "atr_multiplier": 2.0, "daily_trailing": "none"}


def bars(last_close, n=60, step=0.004, spread=0.01):
    closes = [last_close - (n - 1 - i) * step for i in range(n)]
    idx = pd.date_range("2026-09-21 09:30", periods=n, freq="5min")
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


def position(timeframe, **over):
    """Entry 90.21, structural stop 79.57 — 1R is $10.64, so 111.49 is +2R."""
    state = {"in_position": True, "source": "bot", "entry_price": 90.21,
             "shares_held": 1, "initial_stop": 79.57,
             "trailing_stop_price": 79.57, "stop_order_id": "stop-1",
             "timeframe": timeframe}
    state.update(over)
    return state


TWO_R = 111.49


def run(state, price=TWO_R, profile=PROFILE, spread=0.01):
    """`spread` sets the bar range and so the ATR, which is what decides how
    far below price the trail lands — wide bars are how the floor is made to
    bind."""
    broker = FakeBroker()
    positions = {"T": state}
    moved = position_mgmt.maybe_ratchet_stop(broker, positions, "T", state,
                                             bars(price, spread=spread),
                                             profile, price)
    return broker, positions, moved


# ============================================ the config

def test_both_profiles_disable_daily_trailing():
    for name in ("Aggressive", "Moderate"):
        assert CONFIG["risk_profiles"][name]["daily_trailing"] == "none"


def test_intraday_trailing_is_untouched_in_config():
    for name in ("Aggressive", "Moderate"):
        assert CONFIG["risk_profiles"][name]["trailing_stop_type"] == "ATR"


# ============================================ a daily position at +2R

def test_a_daily_position_at_two_r_is_never_replaced():
    """The order's case. Deep in profit, and the broker is not called."""
    broker, positions, moved = run(position("daily"))
    assert moved is False
    assert broker.replaced == []


def test_the_structural_stop_and_leg_id_stand():
    """Not trailing means the state the position was opened with survives —
    the stop leg at the broker is the one the entry bracket created."""
    broker, positions, moved = run(position("daily"))
    assert positions["T"]["trailing_stop_price"] == 79.57
    assert positions["T"]["stop_order_id"] == "stop-1"


def test_it_refuses_at_every_r_multiple():
    """No level unlocks it: not +1R, not +5R, not a loss."""
    for price in (75.00, 90.21, 100.85, 111.49, 145.00):
        broker, _, moved = run(position("daily"), price=price)
        assert moved is False, price
        assert broker.replaced == []


def test_the_breakeven_floor_does_not_reach_a_daily_position():
    """A daily position sitting below breakeven stays there — the floor is
    an intraday rule now. This is the part that was HELD; it is the change."""
    broker, positions, moved = run(
        position("daily", trailing_stop_price=85.66))
    assert moved is False
    assert positions["T"]["trailing_stop_price"] == 85.66   # not 90.21


# ============================================ intraday is unchanged

def test_an_intraday_position_at_two_r_still_trails():
    """The existing behaviour, verbatim: the broker IS called and the stop
    moves up."""
    broker, positions, moved = run(position("intraday"))
    assert moved is True
    assert len(broker.replaced) == 1
    order_id, new_stop = broker.replaced[0]
    assert order_id == "stop-1"
    assert new_stop > 79.57
    assert new_stop < TWO_R


def test_an_intraday_position_still_gets_the_breakeven_floor():
    """+1R with an ATR trail below entry must still land ON entry. Wide bars
    (ATR ~ $8) put the raw trail near $84.80 — below the 90.21 breakeven,
    which is exactly the CRCL shape the floor was written for."""
    broker, positions, moved = run(
        position("intraday", trailing_stop_price=85.66), price=100.85,
        spread=4.0)
    assert moved is True
    assert broker.replaced[0][1] == pytest.approx(90.21, abs=0.01)


def test_an_intraday_position_below_one_r_is_still_held_off():
    """The NOK rule survives: the structural stop stands until +1R."""
    broker, _, moved = run(position("intraday"), price=95.00)
    assert moved is False
    assert broker.replaced == []


# ============================================ resolution and fallback

def test_daily_trailing_only_applies_to_daily_positions():
    assert position_mgmt.trailing_type_for({"timeframe": "daily"},
                                           PROFILE) == "none"
    assert position_mgmt.trailing_type_for({"timeframe": "intraday"},
                                           PROFILE) == "atr"


def test_an_absent_key_keeps_the_old_behaviour():
    """A profile that never mentions daily_trailing must trail exactly as it
    did before this change — otherwise the key would be a silent switch."""
    legacy = {"trailing_stop_type": "ATR", "trailing_stop_value": 2.0}
    assert position_mgmt.trailing_type_for({"timeframe": "daily"},
                                           legacy) == "atr"
    broker, _, moved = run(position("daily"), profile=legacy)
    assert moved is True


def test_an_unknown_timeframe_reads_as_intraday():
    """Positions opened before the timeframe field existed have no value.
    They must keep trailing, not silently stop being managed."""
    state = position("daily")
    del state["timeframe"]
    assert position_mgmt.trailing_type_for(state, PROFILE) == "atr"
    broker, _, moved = run(state)
    assert moved is True


def test_none_is_handled_as_a_trailing_stop_type_in_its_own_right():
    """Generic handling: a candidate is never computed for type none. The
    old code fell through to the percent rule for anything not 'ATR', which
    would have read 'none' as a 2% trail."""
    assert position_mgmt.compute_trailing_stop(
        bars(100.0), {"trailing_stop_type": "none",
                      "trailing_stop_value": 2.0}, 100.0) is None
    assert position_mgmt.compute_trailing_stop(
        bars(100.0), PROFILE, 100.0, trailing_type="none") is None


def test_the_type_is_read_case_insensitively():
    for spelling in ("none", "None", "NONE"):
        assert position_mgmt.trailing_type_for(
            {"timeframe": "daily"},
            {"daily_trailing": spelling}) == "none"


def test_ownership_still_comes_first():
    """A CEO position is not trailed whatever the profile says — the
    ownership boundary must not have moved."""
    state = position("intraday", source="ceo")
    broker, _, moved = run(state)
    assert moved is False
    assert broker.replaced == []
