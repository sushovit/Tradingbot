"""
Goal 7 — exit reconciliation tests. Broker fully mocked, no network.

Verifies:
  - a broker SELL fill is journaled exactly once with the ACTUAL fill price
  - PnL is computed against the journaled BUY and linked to the decision
  - a second sync is a no-op (idempotent via broker_order_id + last_sync)
  - exits already journaled live by the bot are backfilled, not duplicated
"""

import json
import sqlite3
from datetime import datetime, timezone

import pytest

import orders


class FakeClosedOrder:
    def __init__(self, id, symbol, side="sell", qty=5, price=110.0,
                 order_type="stop"):
        self.id = id
        self.symbol = symbol
        self.side = side
        self.filled_qty = qty
        self.filled_avg_price = price
        self.filled_at = datetime.now(timezone.utc)
        self.order_type = order_type


class FakeBroker:
    def __init__(self, closed_orders):
        self._closed = closed_orders
        self.calls = 0

    def get_closed_orders_since(self, since_utc):
        self.calls += 1
        return self._closed


@pytest.fixture
def positions_file(tmp_path, monkeypatch):
    f = tmp_path / "positions.json"
    f.write_text(json.dumps({"NVDA": {"in_position": True, "entry_price": 100.0,
                                      "shares_held": 5}}))
    monkeypatch.setattr(orders, "POSITIONS_STATE_FILE", str(f))
    return f


def _rows(journal_mod, query):
    conn = sqlite3.connect(journal_mod.DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(query).fetchall()
    conn.close()
    return rows


def test_sell_fill_journaled_once_with_pnl_and_outcome(temp_journal, positions_file):
    # The originating decision + BUY, as the bot would have journaled them.
    decision_id = temp_journal.log_decision(
        "NVDA", "trend_continuation", {}, {"approved": True, "conviction_score": 80})
    temp_journal.log_trade("NVDA", "BUY", 5, 100.0, reason="trend_continuation",
                           decision_id=decision_id)

    broker = FakeBroker([FakeClosedOrder("ord-1", "NVDA", price=110.0, qty=5,
                                         order_type="stop")])
    assert orders.sync(broker=broker) == 0

    sells = _rows(temp_journal, "SELECT * FROM trades WHERE action='SELL'")
    assert len(sells) == 1
    sell = sells[0]
    assert sell["price"] == 110.0                       # ACTUAL fill price
    assert sell["pnl_usd"] == pytest.approx(50.0)       # (110-100) * 5
    assert sell["pnl_pct"] == pytest.approx(10.0)
    assert sell["broker_order_id"] == "ord-1"
    assert "Stop Loss" in sell["reason"]

    # Outcome linked back to the originating decision.
    dec = _rows(temp_journal, f"SELECT * FROM decisions WHERE id={decision_id}")[0]
    assert dec["outcome_pnl_usd"] == pytest.approx(50.0)
    assert dec["outcome_trade_id"] == sell["id"]

    # Local position state cleared.
    positions = json.loads(positions_file.read_text())
    assert positions["NVDA"]["in_position"] is False

    # last_sync recorded.
    assert temp_journal.get_meta("last_sync") is not None


def test_second_sync_is_noop(temp_journal, positions_file):
    decision_id = temp_journal.log_decision(
        "NVDA", "trend_continuation", {}, {"approved": True, "conviction_score": 80})
    temp_journal.log_trade("NVDA", "BUY", 5, 100.0, decision_id=decision_id)
    broker = FakeBroker([FakeClosedOrder("ord-1", "NVDA", price=110.0, qty=5)])

    orders.sync(broker=broker)
    orders.sync(broker=broker)   # same fill returned again (overlap window)

    sells = _rows(temp_journal, "SELECT * FROM trades WHERE action='SELL'")
    assert len(sells) == 1       # never double-journaled


def test_live_journaled_exit_backfilled_not_duplicated(temp_journal, positions_file):
    # The bot journaled this exit live (no broker_order_id) before sync ran.
    temp_journal.log_trade("NVDA", "BUY", 5, 100.0)
    temp_journal.log_trade("NVDA", "SELL", 5, 110.0, pnl_usd=50.0, pnl_pct=10.0,
                           reason="Profit Target")

    broker = FakeBroker([FakeClosedOrder("ord-9", "NVDA", price=110.0, qty=5,
                                         order_type="limit")])
    orders.sync(broker=broker)

    sells = _rows(temp_journal, "SELECT * FROM trades WHERE action='SELL'")
    assert len(sells) == 1                              # backfilled, not duplicated
    assert sells[0]["broker_order_id"] == "ord-9"


def test_buy_fills_ignored(temp_journal, positions_file):
    broker = FakeBroker([FakeClosedOrder("ord-2", "NVDA", side="buy", price=100.0)])
    orders.sync(broker=broker)
    assert _rows(temp_journal, "SELECT * FROM trades") == []
