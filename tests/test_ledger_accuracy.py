"""
W4 (PM_PLAN.md): ledger accuracy. No network — the broker is faked.

The work order's premise turned out to be inverted. It read: "SLB is
journaled at 55.86, actual fill 55.745 (positions.json)". Alpaca disagrees —
the BUY order's filled_avg_price IS 55.86, the position's avg_entry_price is
55.86, and cost_basis 279.30 is 5 x 55.86. The JOURNAL was right and
positions.json was the stale one, on BOTH open positions:

    CRCL  positions.json 90.21   broker/journal 90.32
    SLB   positions.json 55.745  broker/journal 55.86

That is not cosmetic. entry_price is what the breakeven floor ratchets to,
so both "risk-free" stops were set about a cent per share UNDER water.
`fix_buy_fill` had corrected the journal; nothing corrected live state.
"""

import json

import pytest

import orders
import streamlit_app as app


class FakeOrder:
    def __init__(self, oid="o1", status="filled", filled_avg_price=None):
        self.id = oid
        self.status = status
        self.filled_avg_price = filled_avg_price


class FakePosition:
    def __init__(self, symbol, avg_entry_price, qty=1):
        self.symbol = symbol
        self.avg_entry_price = avg_entry_price
        self.qty = qty


class FakeBroker:
    def __init__(self, positions=(), orders_by_id=None):
        self._positions = list(positions)
        self._orders = orders_by_id or {}
        self.get_order_calls = 0

    def get_positions(self):
        return self._positions

    def get_order(self, order_id):
        self.get_order_calls += 1
        seq = self._orders.get(str(order_id))
        if isinstance(seq, list):
            return seq.pop(0) if len(seq) > 1 else seq[0]
        return seq


# ==================================================== (1) entry-price drift

def test_sync_corrects_a_stale_entry_price_from_the_broker():
    """The live bug: journal and broker agreed on 90.32, positions.json still
    held 90.21, and the breakeven stop was built from the stale number."""
    positions = {"CRCL": {"in_position": True, "entry_price": 90.21},
                 "SLB": {"in_position": True, "entry_price": 55.745}}
    broker = FakeBroker([FakePosition("CRCL", 90.32),
                         FakePosition("SLB", 55.86, qty=5)])

    fixed = orders.reconcile_entry_prices(broker, positions)

    assert sorted(t for t, _, _ in fixed) == ["CRCL", "SLB"]
    assert positions["CRCL"]["entry_price"] == pytest.approx(90.32)
    assert positions["SLB"]["entry_price"] == pytest.approx(55.86)


def test_reconciliation_is_idempotent():
    positions = {"CRCL": {"in_position": True, "entry_price": 90.32}}
    broker = FakeBroker([FakePosition("CRCL", 90.32)])
    assert orders.reconcile_entry_prices(broker, positions) == []


def test_sub_cent_noise_is_not_a_correction():
    positions = {"CRCL": {"in_position": True, "entry_price": 90.3201}}
    broker = FakeBroker([FakePosition("CRCL", 90.32)])
    assert orders.reconcile_entry_prices(broker, positions) == []


def test_closed_and_unknown_positions_are_left_alone():
    positions = {"OLD": {"in_position": False, "entry_price": 1.0},
                 "GONE": {"in_position": True, "entry_price": 2.0}}
    broker = FakeBroker([FakePosition("OTHER", 9.0)])
    assert orders.reconcile_entry_prices(broker, positions) == []
    assert positions["OLD"]["entry_price"] == 1.0
    assert positions["GONE"]["entry_price"] == 2.0


def test_a_broker_outage_does_not_wipe_local_state():
    class Broken(FakeBroker):
        def get_positions(self):
            raise RuntimeError("alpaca down")

    positions = {"CRCL": {"in_position": True, "entry_price": 90.21}}
    assert orders.reconcile_entry_prices(Broken(), positions) == []
    assert positions["CRCL"]["entry_price"] == 90.21     # untouched


def test_the_correction_restores_a_genuinely_risk_free_stop():
    """Why it matters, stated as arithmetic: the floor ratchets to
    entry_price, so a stale entry puts the 'breakeven' stop under water."""
    positions = {"SLB": {"in_position": True, "entry_price": 55.745}}
    broker = FakeBroker([FakePosition("SLB", 55.86, qty=5)])
    stop_before = positions["SLB"]["entry_price"]
    orders.reconcile_entry_prices(broker, positions)
    stop_after = positions["SLB"]["entry_price"]
    assert stop_before < 55.86 and stop_after == pytest.approx(55.86)
    assert round((stop_before - 55.86) * 5, 2) == -0.58   # the loss avoided


# ==================================================== (2) exit fill polling

def test_exit_fill_is_confirmed_from_the_order_not_the_bar_close():
    broker = FakeBroker(orders_by_id={
        "x1": FakeOrder("x1", "filled", filled_avg_price=101.27)})
    price, confirmed = app.confirm_fill(broker, FakeOrder("x1"),
                                        fallback=100.00)
    assert confirmed is True
    assert price == pytest.approx(101.27)


def test_unconfirmed_exit_falls_back_to_the_reference_price(monkeypatch):
    """An unfilled order must not invent a price — it falls back, and says
    so, which is what lets sync correct it later."""
    monkeypatch.setattr(app.a_time, "sleep", lambda s: None)
    broker = FakeBroker(orders_by_id={"x2": FakeOrder("x2", "new", None)})
    price, confirmed = app.confirm_fill(broker, FakeOrder("x2"),
                                        fallback=100.00, tries=3)
    assert confirmed is False
    assert price == pytest.approx(100.00)


def test_confirm_fill_survives_a_broker_error(monkeypatch):
    monkeypatch.setattr(app.a_time, "sleep", lambda s: None)

    class Boom(FakeBroker):
        def get_order(self, order_id):
            raise app.BrokerError("timeout")

    price, confirmed = app.confirm_fill(Boom(), FakeOrder("x3"),
                                        fallback=42.0, tries=2)
    assert (price, confirmed) == (42.0, False)


def test_an_order_without_an_id_falls_back_immediately():
    class NoId:
        id = None

    broker = FakeBroker()
    assert app.confirm_fill(broker, NoId(), fallback=7.0) == (7.0, False)
    assert broker.get_order_calls == 0


def test_hard_exit_and_manual_close_both_use_the_confirmed_price():
    """Source assertion: both discretionary close paths must route through
    confirm_fill, not journal the last 5-minute bar close."""
    with open("streamlit_app.py", encoding="utf-8") as f:
        body = f.read()
    for marker in ('"Hard Exit Date"', '"Manual Override"'):
        idx = body.index(marker)
        window = body[max(0, idx - 400):idx]
        assert "confirm_fill(broker, close_order" in window, marker
        assert "exit_px" in body[idx - 200:idx]


# ==================================================== entry order id

def test_bot_entries_journal_their_broker_order_id():
    """Without it, sync could only guess which BUY row a fill belonged to by
    matching (ticker, qty)."""
    with open("streamlit_app.py", encoding="utf-8") as f:
        body = f.read()
    idx = body.index('journal.log_trade(ticker, "BUY", qty, fill_price')
    assert "broker_order_id=str(order.id)" in body[idx:idx + 400]
