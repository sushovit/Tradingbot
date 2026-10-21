"""CEO 2026-10-21 - the owner's only control is SELL. It must cancel the
stop AND target legs (held legs included), sell at market and journal
"Owner close"."""

import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace as N

import pytest

import broker as B
import trade_table


class FakeTrading:
    """Orders for one symbol: a NEW target leg and a HELD stop leg (status
    =open queries never return the held one). A cancel goes through
    pending_cancel before it settles."""

    def __init__(self):
        self.orders = {"tgt": "new", "stp": "held", "other": "filled"}
        self.cancelled, self.closed, self.calls = [], [], 0

    def get_orders(self, req):
        self.calls += 1
        for k, v in list(self.orders.items()):          # settle one poll later
            if v == "pending_cancel":
                self.orders[k] = "canceled"
        return [N(id=k, status=f"OrderStatus.{v.upper()}") for k, v in self.orders.items()]

    def cancel_order_by_id(self, oid):
        self.cancelled.append(oid)
        self.orders[oid] = "pending_cancel"

    def close_position(self, ticker):
        working = [k for k, v in self.orders.items()
                   if v in ("new", "held", "pending_cancel", "accepted")]
        if working:
            raise RuntimeError(f"insufficient qty available: {working} reserve shares")
        self.closed.append(ticker)
        return N(id="close-1")


def broker_with(trading):
    b = B.Broker.__new__(B.Broker)
    b.trading = trading
    return b


def test_close_cancels_the_held_stop_leg_and_waits_for_the_cancels(monkeypatch):
    monkeypatch.setattr(B.time, "sleep", lambda s: None)
    t = FakeTrading()
    out = broker_with(t).close_position("REGN")
    assert sorted(t.cancelled) == ["stp", "tgt"]          # held leg too
    assert t.closed == ["REGN"] and out.id == "close-1"


def test_the_old_open_only_cancel_would_have_failed():
    t = FakeTrading()
    t.cancel_order_by_id("tgt")                         # what status=open saw
    t.get_orders(None)
    with pytest.raises(RuntimeError, match="insufficient qty"):
        t.close_position("REGN")


class OwnerBroker:
    def __init__(self):
        self.closed = []

    def close_position(self, ticker):
        self.closed.append(ticker)
        return N(id="close-9")

    def get_order(self, oid):
        return N(status="OrderStatus.FILLED", filled_avg_price=861.0)

    def get_latest_price(self, t):
        return 860.0


def test_owner_close_sells_and_journals_owner_close(temp_journal, monkeypatch):
    import streamlit_app as app
    monkeypatch.setattr(app, "journal", temp_journal)
    monkeypatch.setattr(app, "write_positions", lambda p: None)
    monkeypatch.setattr(app, "append_trade_log", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(app.trade_alerts, "alert_exit", lambda *a, **k: sent.append(a))
    did = temp_journal.log_decision("REGN", "mean_reversion_reclaim",
                                    {"entry": 802.61}, {"approved": True, "conviction_score": 80})
    temp_journal.log_trade("REGN", "BUY", 1, 802.61, reason="mean_reversion_reclaim",
                           decision_id=did)
    positions = {"REGN": {"in_position": True, "entry_price": 802.61, "shares_held": 1,
                          "decision_id": did, "trailing_stop_price": 756.25,
                          "profit_target_price": 862.58}}
    b = OwnerBroker()
    px, ok = app.owner_close(b, positions, "REGN", datetime(2026, 10, 21, 11, 0))
    assert (px, ok) == (861.0, True) and b.closed == ["REGN"]
    assert positions["REGN"] == {"in_position": False}
    conn = sqlite3.connect(temp_journal.DB_FILE)
    row = conn.execute("SELECT reason, price, broker_order_id, exit_stop, exit_target "
                       "FROM trades WHERE action='SELL'").fetchone()
    assert row == ("Owner close", 861.0, "close-9", 756.25, 862.58)
    assert trade_table.result_for("Owner close", 58.39) == "owner"
    assert sent                                          # Discord exit alert queued


@pytest.mark.parametrize("alive,open_,route", [
    (True, True, "queue"), (True, False, "queue"),
    (False, True, "direct"), (False, False, "queue_closed")])
def test_sell_route(alive, open_, route):
    import streamlit_app as app
    assert app.sell_route(alive, open_) == route


def test_queued_sell_is_the_file_the_worker_reads(tmp_path, monkeypatch):
    import streamlit_app as app
    monkeypatch.chdir(tmp_path)
    path = app.queue_owner_sell("REGN")
    assert path == "sell_REGN.command" and (tmp_path / path).exists()
    src = open(app.__file__, encoding="utf-8").read()
    block = src[src.index("# 3) Owner SELL"):src.index("# 4) Trailing stop")]
    assert "SELL_COMMAND.format(ticker)" in block and "owner_close(" in block
    assert "os.remove(manual_sell_command_file)" in block


def test_the_dashboard_offers_sell_with_a_confirm_step():
    src = open("streamlit_app.py", encoding="utf-8").read()
    ui = src[src.index("def _render_owner_sell"):src.index("def _execute_owner_sell")]
    assert "owner_sell_confirm" in ui and "Yes, sell" in ui and "Cancel" in ui
    assert "if not desk_config.in_worker():\n            _render_owner_sell(" in src
