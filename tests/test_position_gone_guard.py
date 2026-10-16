"""
The 2026-10-15 09:33:22 sequence, and the review timeout regression.

REGN: bracket submitted 09:32, NOT filled until 09:34:32 - get_positions()
did not list it, and one empty read cleared a position the broker then
held. TXN: its stop leg was "partially_filled" (3 of 4 @ 278.78) at 09:33:22
and the whole exit was journaled at the partial's price; the last share
filled at 277.79 at 09:34:45 (true avg 278.5325).
"""

import json
import os
import shutil
from datetime import datetime, timedelta, timezone

import pandas as pd

import streamlit_app as app

ROOT = os.path.join(os.path.dirname(__file__), "..")
UTC = timezone.utc


class Order:
    def __init__(self, id, status, side="sell", order_type="stop", fill=None,
                 filled_at=None, symbol="REGN", qty=1, filled_qty=None):
        self.id, self.status, self.side, self.order_type = id, status, side, order_type
        self.filled_avg_price, self.filled_at, self.symbol = fill, filled_at, symbol
        self.qty, self.filled_qty = qty, filled_qty


def regn_state():
    return {"in_position": True, "source": "bot", "entry_price": 806.24,
            "shares_held": 1, "initial_stop": 756.25,
            "trailing_stop_price": 756.25, "profit_target_price": 956.21,
            "stop_order_id": "regn-stop", "target_order_id": "regn-target",
            "entry_order_id": "regn-entry", "decision_id": 12677,
            "prompt_version": 5, "setup": "mean_reversion_reclaim",
            "timeframe": "daily"}


class OneOrderBroker:
    def __init__(self, orders):
        self.orders = orders

    def get_order(self, oid):
        return self.orders[oid]


T_FILL = datetime(2026, 10, 15, 13, 34, 32, tzinfo=UTC)       # 09:34:32 ET


# ================================================== the rules

def test_an_unfilled_entry_is_never_gone():
    """REGN at 09:33:22: entry submitted, not filled."""
    b = OneOrderBroker({"regn-entry": Order("regn-entry", "OrderStatus.NEW",
                                            side="buy", order_type="market")})
    state = regn_state()
    for _ in range(5):                                   # however many reads
        gone, why = app.confirm_position_gone(b, "REGN", state,
                                              T_FILL - timedelta(minutes=1))
        assert gone is False and "not filled yet" in why
    assert "gone_reads" not in state


def test_an_entry_filled_under_15_minutes_ago_is_never_gone():
    b = OneOrderBroker({"regn-entry": Order("regn-entry", "filled", side="buy",
                                            fill=802.61, filled_at=T_FILL)})
    state = regn_state()
    for minutes in (0.5, 5, 14.9):
        gone, why = app.confirm_position_gone(b, "REGN", state,
                                              T_FILL + timedelta(minutes=minutes))
        assert gone is False and "grace" in why


def test_two_consecutive_missing_reads_are_needed():
    b = OneOrderBroker({"regn-entry": Order("regn-entry", "filled", side="buy",
                                            fill=802.61, filled_at=T_FILL)})
    state = regn_state()
    later = T_FILL + timedelta(hours=2)
    assert app.confirm_position_gone(b, "REGN", state, later)[0] is False
    assert state["gone_reads"] == 1
    assert app.confirm_position_gone(b, "REGN", state, later)[0] is True


def test_a_legacy_record_without_an_entry_id_still_needs_two_reads():
    state = regn_state(); state.pop("entry_order_id")
    b = OneOrderBroker({})
    assert app.confirm_position_gone(b, "REGN", state, T_FILL)[0] is False
    assert app.confirm_position_gone(b, "REGN", state, T_FILL)[0] is True


def test_a_partially_filled_leg_is_not_an_exit():
    state = {"stop_order_id": "txn-stop", "target_order_id": "txn-target"}
    b = OneOrderBroker({
        "txn-stop": Order("txn-stop", "OrderStatus.PARTIALLY_FILLED", fill=278.78,
                          symbol="TXN", qty=4, filled_qty=3),
        "txn-target": Order("txn-target", "new", order_type="limit", symbol="TXN")})
    assert app.detect_filled_exit(b, "TXN", state) == (None, None, None)
    b.orders["txn-stop"] = Order("txn-stop", "filled", fill=278.5325,
                                 symbol="TXN", qty=4, filled_qty=4)
    assert app.detect_filled_exit(b, "TXN", state) == (278.5325, "Stop Loss",
                                                       "txn-stop")


def test_exit_history_never_matches_an_older_positions_sell():
    old = Order("old-sell", "filled", fill=700.0, symbol="REGN",
                filled_at=T_FILL - timedelta(days=2))

    class B:
        def get_closed_orders_since(self, since):
            return [old]
    assert app.resolve_exit_fill(B(), "REGN", after_utc=T_FILL)[0] is None
    assert app.resolve_exit_fill(B(), "REGN")[0] == 700.0


def test_reconcile_adopt_keeps_the_held_stop_leg():
    class P:
        symbol, qty, avg_entry_price = "REGN", "1", "802.61"

    class B:
        def get_positions(self):
            return [P()]

        def get_live_orders(self, symbol=None):
            return [Order("t", "new", order_type="limit"),
                    Order("s", "held", order_type="stop")]
    book = app.reconcile_positions(B(), {})
    assert book["REGN"]["stop_order_id"] == "s"
    assert book["REGN"]["target_order_id"] == "t"


# ================================================== the real loop, replayed

class Pos:
    def __init__(self, symbol, qty, avg):
        self.symbol, self.qty, self.avg_entry_price = symbol, str(qty), str(avg)
        self.current_price, self.unrealized_pl = avg, 0.0
        self.market_value = qty * avg


class SequenceBroker:
    """Cycle 1 = 09:33:22: REGN entry NEW (not listed); TXN stop 3/4 filled
    (TXN still listed with 1 share). Cycle 2 = 09:34:50: REGN entry filled
    and listed; TXN stop fully filled @ 278.5325, TXN no longer listed."""

    def __init__(self):
        self.cycle = 0

    def orders(self):
        c2 = self.cycle >= 2
        return {
            "regn-entry": Order("regn-entry", "filled" if c2 else "new",
                                side="buy", order_type="market",
                                fill=802.61 if c2 else None,
                                filled_at=T_FILL if c2 else None),
            "regn-stop": Order("regn-stop", "held"),
            "regn-target": Order("regn-target", "new", order_type="limit"),
            "txn-stop": Order("txn-stop", "filled" if c2 else "partially_filled",
                              fill=278.5325 if c2 else 278.78, symbol="TXN",
                              qty=4, filled_qty=4 if c2 else 3,
                              filled_at=T_FILL + timedelta(seconds=13)),
            "txn-target": Order("txn-target", "canceled" if c2 else "new",
                                order_type="limit", symbol="TXN"),
        }

    def get_order(self, oid):
        return self.orders()[oid]

    def get_positions(self):
        if self.cycle >= 2:
            return [Pos("REGN", 1, 802.61)]
        return [Pos("TXN", 1, 280.25)]

    def get_equity(self):
        return 5000.0

    def get_live_orders(self, ticker=None):
        return []

    def get_open_orders(self, ticker=None):
        return []

    def get_closed_orders_since(self, since):
        return []

    def get_latest_price(self, t):
        return 805.0 if t == "REGN" else 278.0

    def get_daily_bars(self, tickers, lookback_days=60):
        idx = pd.bdate_range(end="2026-10-14", periods=60, tz="UTC")
        c = [700.0 + i for i in range(60)]
        df = pd.DataFrame({"open": c, "high": c, "low": c, "close": c,
                           "volume": 1}, index=idx)
        return {t: df for t in tickers}

    def get_bars(self, tickers, timeframe_minutes=5, lookback_days=5):
        idx = pd.date_range("2026-10-15 13:30", periods=4, freq="5min", tz="UTC")
        out = {}
        for t in tickers:
            px = 805.0 if t == "REGN" else 278.5
            out[t] = pd.DataFrame({"open": px, "high": px, "low": px,
                                   "close": px, "volume": 100}, index=idx)
        return out


def test_the_0933_sequence_through_the_real_worker_loop(temp_journal, tmp_path,
                                                       monkeypatch):
    import claude_integration
    import local_analyst
    import session_clock
    import universe

    shutil.copy(os.path.join(ROOT, "bot_config.json"), tmp_path / "bot_config.json")
    monkeypatch.chdir(tmp_path)
    did = temp_journal.log_decision("TXN", "mean_reversion_reclaim",
                                    {"stop": 278.0}, {"approved": True})
    temp_journal.log_trade("TXN", "BUY", 4, 280.25, decision_id=did)
    txn = {"in_position": True, "source": "bot", "entry_price": 280.25,
           "shares_held": 4, "initial_stop": 278.0, "trailing_stop_price": 278.0,
           "profit_target_price": 298.69, "stop_order_id": "txn-stop",
           "target_order_id": "txn-target", "decision_id": did,
           "setup": "mean_reversion_reclaim", "timeframe": "daily"}
    json.dump({"REGN": regn_state(), "TXN": txn}, open("positions.json", "w"))
    open(app.LOCK_FILE, "w").write("1")

    broker = SequenceBroker()
    statuses = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            t = datetime(2026, 10, 15, 9, 33, 22) if broker.cycle <= 1 \
                else datetime(2026, 10, 15, 9, 34, 50)
            return app.EASTERN_TZ.localize(t) if tz else t

    def sleep(_):
        if broker.cycle >= 2 and os.path.exists(app.LOCK_FILE):
            os.remove(app.LOCK_FILE)
        broker.cycle += 1

    broker.cycle = 1
    monkeypatch.setattr(app, "Broker", lambda: broker)
    monkeypatch.setattr(app, "datetime", Clock)
    monkeypatch.setattr(app.a_time, "sleep", sleep)
    monkeypatch.setattr(app, "write_status", lambda m, *a, **k: statuses.append(m))
    monkeypatch.setattr(app, "reconcile_positions", lambda b, p: p)
    monkeypatch.setattr(app, "refresh_order_ids", lambda b, p: [])
    monkeypatch.setattr(universe, "refresh", lambda *a, **k: [])
    monkeypatch.setattr(universe, "load_universe_tickers", lambda *a, **k: [])
    monkeypatch.setattr(local_analyst, "ensure_ollama", lambda: {
        "up": True, "detail": "stub", "started": False, "warmed": True})
    monkeypatch.setattr(claude_integration, "credit_preflight", lambda: {"ok": True})
    monkeypatch.setattr(session_clock, "keep_awake", lambda on: True)
    monkeypatch.setattr(session_clock, "suspend_gap", lambda *a, **k: None)
    monkeypatch.setattr(app.trade_alerts, "alert_exit", lambda *a, **k: None)
    monkeypatch.setattr(app.trade_alerts, "alert_startup", lambda *a, **k: None)
    monkeypatch.setattr(app, "maybe_manage_open_position", lambda *a, **k: None,
                        raising=False)

    app._worker_loop()

    cycles = [s for s in statuses if s.startswith("Cycle #")]
    assert len(cycles) == 2, statuses
    # cycle 1 (09:33:22): REGN kept - entry not filled; TXN kept - partial
    assert "REGN: Entry pending (new)" in cycles[0]
    assert "TXN: SOLD" not in cycles[0]
    book = json.load(open("positions.json"))
    assert book["REGN"]["in_position"] is True           # never cleared
    assert book["TXN"]["in_position"] is False           # exited in cycle 2
    import sqlite3
    sell = sqlite3.connect(temp_journal.DB_FILE).execute(
        "SELECT qty, price, round(pnl_usd, 2) FROM trades WHERE ticker='TXN' "
        "AND action='SELL'").fetchall()
    assert sell == [(4.0, 278.5325, -6.87)]              # full fill, true avg
    assert "TXN: SOLD (Stop Loss)" in cycles[1]


# ================================================== D: review timeout

def test_review_has_its_own_600s_streamed_client_and_gatekeeper_keeps_60(monkeypatch):
    import claude_integration as ci
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-0000000000")
    monkeypatch.setattr(ci, "_review_client", None)
    monkeypatch.setattr(ci, "_claude_client", None)
    review = ci._get_review_client()
    assert review.timeout.read == 600 and review.timeout.connect == 5
    assert review.max_retries == 0
    assert ci._get_client().timeout == 60                # gatekeeper unchanged
    import review_bot
    assert review_bot.MAX_RETRIES == 1
    src = open(os.path.join(ROOT, "review_bot.py"), encoding="utf-8").read()
    assert "_get_review_client()" in src and "messages.stream(" in src


class _Stream:
    def __init__(self, outcome):
        self.outcome = outcome

    def __enter__(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.outcome


def _message(text):
    class Block:
        type = "text"
    b = Block(); b.text = text

    class Usage:
        output_tokens, input_tokens = 900, 100
    class Msg:
        content, stop_reason, usage = [b], "end_turn", Usage()
    return Msg()


def test_review_retries_a_timeout_once_then_gives_up(monkeypatch):
    import claude_integration as ci
    import review_bot
    calls = []

    class Client:
        class messages:
            @staticmethod
            def stream(**kw):
                calls.append(kw)
                return _Stream(TimeoutError("Request timed out"))
    monkeypatch.setattr(ci, "_get_review_client", lambda: Client())
    monkeypatch.setattr(review_bot.time, "sleep", lambda s: None)
    out = review_bot.request_review({"date": "2026-10-15", "clock": ""})
    assert "error" in out and len(calls) == 2          # one try + one retry


def test_review_succeeds_on_the_retry(monkeypatch):
    import claude_integration as ci
    import review_bot
    outcomes = [TimeoutError("Request timed out"),
                _message("## 1. a\n## 2. b\n## 3. c\n## 4. d\n## 5. e\nok")]

    class Client:
        class messages:
            @staticmethod
            def stream(**kw):
                return _Stream(outcomes.pop(0))
    monkeypatch.setattr(ci, "_get_review_client", lambda: Client())
    monkeypatch.setattr(review_bot.time, "sleep", lambda s: None)
    out = review_bot.request_review({"date": "2026-10-15", "clock": ""})
    assert out.get("text", "").count("## 5.") == 1
