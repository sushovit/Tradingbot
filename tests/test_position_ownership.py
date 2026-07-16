"""
Goal 11 — position ownership. The bot must never manage CEO positions.

Incident 2026-07-15: the bot adopted a CEO order-sheet BAC swing position at
reconciliation and its 5-min ATR trail replaced the sheet's designed stop
(59.60 -> 60.98). These tests pin the ownership boundary:

  - a bot cycle with a CEO position present leaves its stop order untouched
  - a bot position's trail still ratchets
  - unknown (reconciliation-adopted) positions are display-only too
  - orders.py marks its entries source="ceo"
"""

import pandas as pd
import pytest

import position_mgmt
import orders


def trending_df():
    """Rising 5-min bars — the ATR trail WOULD ratchet if allowed."""
    closes = [100.0 + i * 0.4 for i in range(40)]
    idx = pd.date_range("2026-07-15 09:30", periods=len(closes), freq="5min")
    return pd.DataFrame({
        "open": [c - 0.1 for c in closes],
        "high": [c + 0.2 for c in closes],
        "low": [c - 0.3 for c in closes],
        "close": closes,
        "volume": [100_000] * len(closes),
    }, index=idx)


RISK_PROFILE = {"trailing_stop_type": "ATR", "trailing_stop_value": 2.0}


class FakeOrder:
    def __init__(self, id):
        self.id = id


class FakeBroker:
    def __init__(self):
        self.replaced = []   # (order_id, new_stop)

    def replace_stop(self, order_id, new_stop):
        self.replaced.append((order_id, new_stop))
        return FakeOrder(f"{order_id}-replaced")


def make_state(source, stop=95.0):
    return {"in_position": True, "source": source, "entry_price": 100.0,
            "shares_held": 5, "trailing_stop_price": stop,
            "profit_target_price": 120.0, "stop_order_id": "stop-1"}


def test_ceo_position_stop_untouched():
    broker = FakeBroker()
    positions = {"BAC": make_state("ceo", stop=59.60)}
    changed = position_mgmt.maybe_ratchet_stop(
        broker, positions, "BAC", positions["BAC"], trending_df(),
        RISK_PROFILE, current_price=115.0)
    assert changed is False
    assert broker.replaced == []                       # leg never touched
    assert positions["BAC"]["trailing_stop_price"] == 59.60
    assert positions["BAC"]["stop_order_id"] == "stop-1"


def test_unknown_position_stop_untouched():
    broker = FakeBroker()
    positions = {"XYZ": make_state("unknown")}
    changed = position_mgmt.maybe_ratchet_stop(
        broker, positions, "XYZ", positions["XYZ"], trending_df(),
        RISK_PROFILE, current_price=115.0)
    assert changed is False
    assert broker.replaced == []


def test_missing_source_defaults_to_unmanaged():
    state = make_state("bot")
    del state["source"]                                # legacy row, no tag
    broker = FakeBroker()
    changed = position_mgmt.maybe_ratchet_stop(
        broker, {"XYZ": state}, "XYZ", state, trending_df(),
        RISK_PROFILE, current_price=115.0)
    assert changed is False
    assert broker.replaced == []


def test_bot_position_trail_still_ratchets():
    broker = FakeBroker()
    positions = {"NVDA": make_state("bot", stop=95.0)}
    changed = position_mgmt.maybe_ratchet_stop(
        broker, positions, "NVDA", positions["NVDA"], trending_df(),
        RISK_PROFILE, current_price=115.0)
    assert changed is True
    assert len(broker.replaced) == 1
    order_id, new_stop = broker.replaced[0]
    assert order_id == "stop-1"
    assert 95.0 < new_stop < 115.0                     # ratcheted up, below price
    assert positions["NVDA"]["trailing_stop_price"] == pytest.approx(new_stop)
    assert positions["NVDA"]["stop_order_id"] == "stop-1-replaced"


def test_bot_trail_never_ratchets_down():
    broker = FakeBroker()
    # Stop already ABOVE what the ATR rule would produce -> no replacement.
    positions = {"NVDA": make_state("bot", stop=114.5)}
    changed = position_mgmt.maybe_ratchet_stop(
        broker, positions, "NVDA", positions["NVDA"], trending_df(),
        RISK_PROFILE, current_price=115.0)
    assert changed is False
    assert broker.replaced == []


def test_ceo_sheet_entry_tagged_ceo(temp_journal, tmp_path, monkeypatch):
    monkeypatch.setattr(orders, "POSITIONS_STATE_FILE", str(tmp_path / "pos.json"))

    class FakeLeg:
        def __init__(self, id, order_type):
            self.id, self.order_type = id, order_type

    class FakeBracket:
        def __init__(self):
            self.id = "br-1"
            self.status = "filled"
            self.filled_avg_price = 60.0
            self.legs = [FakeLeg("s1", "stop"), FakeLeg("t1", "limit")]

    class FakeExecBroker:
        def submit_bracket(self, ticker, qty, stop, target):
            return FakeBracket()

        def get_order(self, order_id):
            return FakeBracket()

    positions = {}
    orders.execute_order(
        {"action": "BUY", "ticker": "BAC", "notional_usd": 430,
         "entry": 61.56, "stop": 59.60, "target": 65.50,
         "setup": "momentum_continuation"},
        FakeExecBroker(), decision_id=None, positions=positions)
    assert positions["BAC"]["source"] == "ceo"
    assert position_mgmt.is_bot_managed(positions["BAC"]) is False
