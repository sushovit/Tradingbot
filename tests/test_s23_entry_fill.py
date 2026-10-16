"""
S23 (CEO, 2026-10-15) — the position is the broker's fill, never the signal
reference.

TXN 10-14: signal reference 282.815, broker fill 280.25, structural stop
278.00. The worker's poll compared str(OrderStatus.FILLED) - which is
"OrderStatus.FILLED" - to "filled", never matched, and kept the reference.
R became 4.815 instead of 2.25, so +1R sat at 287.63 instead of 282.50, and
TXN's 283.05 close never moved the stop.
"""

import os

from alpaca.trading.enums import OrderStatus

import streamlit_app as app
import trade_alerts as ta
from test_r_atr_s25 import m5, run, txn

ROOT = os.path.join(os.path.dirname(__file__), "..")


class O:
    def __init__(self, status, fill=None, filled_qty=None, legs=()):
        self.id, self.status = "entry-1", status
        self.filled_avg_price, self.filled_qty, self.legs = fill, filled_qty, list(legs)


class Leg:
    def __init__(self, id, order_type):
        self.id, self.order_type = id, order_type


class PollBroker:
    def __init__(self, seq):
        self.seq = list(seq)

    def get_order(self, oid):
        return self.seq.pop(0) if len(self.seq) > 1 else self.seq[0]


# ================================================== the root cause

def test_the_old_comparison_could_never_match():
    assert str(OrderStatus.FILLED).lower() != "filled"        # the bug
    assert app._status(O(OrderStatus.FILLED)) == "filled"     # the fix


def test_poll_reads_a_real_enum_fill():
    b = PollBroker([O(OrderStatus.NEW),
                    O(OrderStatus.FILLED, fill="280.25", filled_qty="4",
                      legs=[Leg("s", "OrderType.STOP"), Leg("t", "OrderType.LIMIT")])])
    price, qty, ok, status, legs = app.poll_entry_fill(
        b, O(OrderStatus.NEW), 282.815, 4, sleep=lambda s: None)
    assert (price, qty, ok, status) == (280.25, 4.0, True, "filled")
    assert legs == {"stop": "s", "target": "t"}


def test_poll_takes_a_partial_entry_at_its_avg_and_filled_qty():
    b = PollBroker([O(OrderStatus.PARTIALLY_FILLED, fill="280.30", filled_qty="3")])
    price, qty, ok, status, _ = app.poll_entry_fill(
        b, O(OrderStatus.NEW), 282.815, 4, tries=3, sleep=lambda s: None)
    assert (price, qty, ok, status) == (280.30, 3.0, True, "partially_filled")


def test_poll_unfilled_keeps_the_reference_but_says_so():
    b = PollBroker([O(OrderStatus.NEW)])
    price, qty, ok, status, _ = app.poll_entry_fill(
        b, O(OrderStatus.NEW), 806.24, 1, tries=3, sleep=lambda s: None)
    assert (price, qty, ok, status) == (806.24, 1, False, "new")


def test_confirm_fill_reads_a_real_enum_fill():
    b = PollBroker([O(OrderStatus.FILLED, fill="278.5325")])
    assert app.confirm_fill(b, O(OrderStatus.NEW), 280.0, tries=1) == (278.5325, True)


# ================================================== the broker reconcile

def test_reconcile_moves_txn_to_the_broker_fill():
    s = txn(entry_price=282.815)
    what = app.reconcile_entry_with_broker(s, 280.25, 4.0)
    assert what == "entry 282.815 -> 280.25"
    assert s["entry_price"] == 280.25 and s["signal_price"] == 282.815
    assert s["original_stop"] == 278.0
    risk = s["entry_price"] - s["original_stop"]
    assert risk == 2.25 and s["entry_price"] + risk == 282.50       # +1R


def test_regn_at_its_fill_is_unchanged():
    s = {"in_position": True, "entry_price": 802.61, "shares_held": 1,
         "initial_stop": 756.25, "original_stop": 756.25}
    before = dict(s)
    assert app.reconcile_entry_with_broker(s, 802.61, 1.0) is None
    assert s == before


def test_a_partial_entry_takes_the_broker_qty():
    s = txn(entry_price=280.25, shares_held=4)
    assert app.reconcile_entry_with_broker(s, 280.30, 3.0) == \
        "entry 280.25 -> 280.3; qty 4 -> 3"
    assert s["shares_held"] == 3.0


# ================================================== the ladder, both ways

def test_at_the_reference_entry_the_283_05_close_moved_nothing():
    """What happened on 10-14: R from 282.815 -> +1R at 287.63."""
    s = txn(entry_price=282.815)
    _, broker, _, _ = run(s, m5([283.05]), 283.10)
    assert broker.replaced == []


def test_at_the_fill_the_283_05_close_raises_the_stop_per_the_live_ladder():
    """Fixed: entry 280.25, R 2.25, +1R = 282.50; the r_atr ladder's +1R
    step is entry - 0.25R = 279.6875 -> 279.69."""
    s = txn(entry_price=282.815)
    app.reconcile_entry_with_broker(s, 280.25, 4.0)
    changed, broker, journaled, _ = run(s, m5([283.05]), 283.10)
    assert changed is True and broker.replaced == [("stop-0", 279.69)]
    assert s["trailing_stop_price"] == 279.69


def test_the_reconcile_runs_before_any_lock_or_trail_math():
    src = open(os.path.join(ROOT, "streamlit_app.py"), encoding="utf-8").read()
    loop = src[src.index("def _worker_loop():"):]
    fix = loop.index("reconcile_entry_with_broker(")
    for call in ("maybe_pct_lock(", "maybe_lock_profit(", "maybe_ratchet_stop("):
        assert fix < loop.index(call), call
    assert 'journal_ops_once("entry_reconciled"' in loop


# ================================================== what the desk is told

def test_the_alert_shows_the_fill_and_never_a_reference_as_a_fill():
    from datetime import datetime
    ts = datetime(2026, 10, 14, 9, 32)
    f = dict(ta.buy_embed("TXN", 4, 280.25, ts, signal_price=282.815)["fields"])
    assert f["Fill"] == "$280.25" and f["Signal"] == "$282.815"
    f = dict(ta.buy_embed("REGN", 1, 806.24, ts, fill_confirmed=False)["fields"])
    assert f["Fill"] == "pending (signal $806.24)" and "Signal" not in f


def test_a_partial_exit_never_shrinks_the_position():
    """TXN 10-15 09:33:22: stop leg 3 of 4 filled, broker holds 1. That is
    an exit in progress - the completed exit must journal all 4."""
    class B:
        def get_order(self, oid):
            return O(OrderStatus.PARTIALLY_FILLED if oid == "stop-0" else OrderStatus.NEW)
    s = txn(entry_price=280.25, shares_held=4, target_order_id="tgt-0")
    assert app.exit_in_progress(B(), s) is True
    assert app.reconcile_entry_with_broker(s, 280.25, 1.0, allow_qty=False) is None
    assert s["shares_held"] == 4


def test_an_untouched_bracket_means_a_partial_entry():
    class B:
        def get_order(self, oid):
            return O(OrderStatus.HELD if oid == "stop-0" else OrderStatus.NEW)
    assert app.exit_in_progress(B(), txn(target_order_id="tgt-0")) is False
