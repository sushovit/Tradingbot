"""
Goal 12b — intern trading account isolation. All mocked, no network.

  (a) intern broker constructed with INTERN keys ONLY; main with main keys
  (b) main-desk code (orders.py, streamlit_app.py) never reaches the intern
      account
  (c) bot position management can never touch intern positions
  (d) verdict without numeric invalidation -> clean journaled rejection
  (e) conviction < 70 -> no trade today (journaled, valid outcome)
  (f) risk-gate rejections journaled with reason
"""

import os
import sqlite3

import pytest

import broker as broker_mod
import intern_trader
import position_mgmt


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# =============================================================================
# (a) key isolation at the broker layer
# =============================================================================

class _CapturingClient:
    captured = []

    def __init__(self, api_key, secret_key, *a, **k):
        _CapturingClient.captured.append((api_key, secret_key))


@pytest.fixture
def fake_clients(monkeypatch):
    _CapturingClient.captured = []
    monkeypatch.setattr(broker_mod, "TradingClient", _CapturingClient)
    monkeypatch.setattr(broker_mod, "StockHistoricalDataClient", _CapturingClient)
    monkeypatch.setattr(broker_mod, "ScreenerClient", _CapturingClient)
    monkeypatch.setenv("ALPACA_API_KEY", "MAIN-KEY")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "MAIN-SECRET")
    monkeypatch.setenv("INTERN_ALPACA_API_KEY", "INTERN-KEY")
    monkeypatch.setenv("INTERN_ALPACA_SECRET_KEY", "INTERN-SECRET")
    return _CapturingClient


def test_intern_broker_uses_intern_keys_only(fake_clients):
    broker_mod.Broker(account="intern")
    assert fake_clients.captured, "no clients constructed"
    for key, secret in fake_clients.captured:
        assert key == "INTERN-KEY" and secret == "INTERN-SECRET"


def test_main_broker_uses_main_keys_only(fake_clients):
    broker_mod.Broker()          # default account="main"
    for key, secret in fake_clients.captured:
        assert key == "MAIN-KEY" and secret == "MAIN-SECRET"


def test_unknown_account_rejected(fake_clients):
    with pytest.raises(ValueError):
        broker_mod.Broker(account="shadow")


def test_intern_trader_pins_intern_account(fake_clients):
    b = intern_trader.get_intern_broker()
    assert b.account == "intern"
    for key, _ in fake_clients.captured:
        assert key == "INTERN-KEY"


# =============================================================================
# (b) main desk can never reach the intern account
# =============================================================================

def test_main_desk_sources_never_reference_intern_account():
    """orders.py's trading paths and the bot WORKER must never construct an
    intern broker. The dashboard's Intern Desk tab may READ the intern
    account (scoreboard) — display is not trading — so only the worker
    section of streamlit_app.py is scanned."""
    with open(os.path.join(ROOT, "streamlit_app.py"), encoding="utf-8") as f:
        src = f.read()
    worker_part = src.split("# --- STREAMLIT UI ---")[0]
    assert 'account="intern"' not in worker_part \
        and "account='intern'" not in worker_part, \
        "bot worker reaches the intern account"
    # orders.py: only the sync() desk branch may mention intern; the order
    # EXECUTION path (execute_order + ingest, i.e. everything before the
    # exit-reconciliation section) must not.
    with open(os.path.join(ROOT, "orders.py"), encoding="utf-8") as f:
        src = f.read()
    exec_part = src.split("def execute_order")[1].split("EXIT RECONCILIATION")[0]
    assert "intern" not in exec_part.lower(), \
        "orders.py execution path references the intern account"


def test_intern_trade_prefix_never_used_by_main_orders(temp_journal, tmp_path,
                                                       monkeypatch):
    """A main-desk CEO BUY journals without the INTERN prefix, so scoreboard
    queries (reason LIKE 'INTERN%') can never mix desks."""
    import orders as orders_mod
    monkeypatch.setattr(orders_mod, "POSITIONS_STATE_FILE", str(tmp_path / "p.json"))

    class FakeLeg:
        def __init__(self, id, order_type):
            self.id, self.order_type = id, order_type

    class FakeBracket:
        id = "br-1"
        status = "filled"
        filled_avg_price = 100.0
        legs = [FakeLeg("s", "stop"), FakeLeg("t", "limit")]

    class FakeBroker:
        def submit_bracket(self, *a):
            return FakeBracket()

        def get_order(self, oid):
            return FakeBracket()

        # Order-side dedupe interface (clean account: no dupes).
        def get_positions(self):
            return []

        def get_live_orders(self, ticker=None):
            return []

    positions = {}
    orders_mod.execute_order(
        {"action": "BUY", "ticker": "NVDA", "notional_usd": 300,
         "entry": 100.0, "stop": 95.0, "target": 110.0,
         "setup": "trend_continuation"},
        FakeBroker(), decision_id=None, positions=positions)
    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.row_factory = sqlite3.Row
    buys = conn.execute("SELECT reason FROM trades WHERE action='BUY'").fetchall()
    conn.close()
    assert buys and not any(r["reason"].startswith("INTERN") for r in buys)


# =============================================================================
# (c) bot management can never touch intern positions
# =============================================================================

def test_bot_management_ignores_non_bot_sources():
    """Intern positions live on a different account, so the bot never even
    sees them; belt-and-braces, any position not tagged source=bot is
    unmanaged (incl. hypothetical 'intern')."""
    for source in ("intern", "ceo", "unknown", None):
        state = {"in_position": True, "source": source}
        assert position_mgmt.is_bot_managed(state) is False
    assert position_mgmt.is_bot_managed({"in_position": True, "source": "bot"})


# =============================================================================
# (d)(e)(f) intern trade-path rejections — all journaled
# =============================================================================

@pytest.fixture(autouse=True)
def _pretend_trading_day(monkeypatch):
    """Trade-path tests simulate a trading day regardless of when the suite
    runs (the weekend guard has its own dedicated tests)."""
    monkeypatch.setattr(intern_trader, "is_trading_day", lambda dt=None: True)

def _rules_rows(j):
    conn = sqlite3.connect(j.DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM decisions WHERE source='intern'").fetchall()
    out = [dict(r) for r in rows]
    conn.close()
    return out


def make_verdict(**over):
    v = {"stance": "long_setup", "setup_name": "momentum_continuation",
         "conviction": 82, "invalidation": 95.0, "key_risk": "r",
         "reasoning": "solid base"}
    v.update(over)
    return v


class FakeAccount:
    equity = "2000"
    last_equity = "2000"


class FakeInternBroker:
    def __init__(self, positions=None, price=100.0):
        self._positions = positions or []
        self._price = price
        self.brackets = []

    def get_equity(self):
        return 2000.0

    def get_account(self):
        return FakeAccount()

    def get_positions(self):
        return self._positions

    def get_latest_price(self, t):
        return self._price

    def submit_bracket(self, ticker, qty, stop, target):
        self.brackets.append((ticker, qty, stop, target))

        class O:
            id = "int-br-1"
            status = "filled"
            filled_avg_price = 100.0
        return O()

    def get_order(self, oid):
        class O:
            id = oid
            status = "filled"
            filled_avg_price = 100.0
        return O()


def test_verdict_without_invalidation_rejected_and_journaled(
        temp_journal, tmp_path, monkeypatch):
    monkeypatch.setattr(intern_trader, "INTERN_POSITIONS_FILE",
                        str(tmp_path / "ip.json"))
    line = intern_trader.execute_trade(
        {"NVDA": make_verdict(invalidation=None)}, broker=FakeInternBroker())
    assert "no numeric invalidation" in line
    rows = _rules_rows(temp_journal)
    assert len(rows) == 1
    assert "no_stop_in_verdict" in rows[0]["verdict"]


def test_conviction_below_70_means_no_trade(temp_journal, tmp_path, monkeypatch):
    monkeypatch.setattr(intern_trader, "INTERN_POSITIONS_FILE",
                        str(tmp_path / "ip.json"))
    broker = FakeInternBroker()
    line = intern_trader.execute_trade(
        {"NVDA": make_verdict(conviction=69)}, broker=broker)
    assert "no trade today" in line
    assert broker.brackets == []                       # nothing submitted
    rows = _rules_rows(temp_journal)
    assert any("no_trade_today" in r["verdict"] for r in rows)


def test_risk_gate_rejection_journaled(temp_journal, tmp_path, monkeypatch):
    monkeypatch.setattr(intern_trader, "INTERN_POSITIONS_FILE",
                        str(tmp_path / "ip.json"))

    class P:
        qty = 5
        avg_entry_price = 100.0
    broker = FakeInternBroker(positions=[P(), P()])    # already at max 2
    line = intern_trader.execute_trade({"NVDA": make_verdict()}, broker=broker)
    assert "REJECTED" in line
    assert broker.brackets == []
    rows = _rules_rows(temp_journal)
    assert any("max_positions_reached" in r["verdict"] for r in rows)


def test_valid_trade_lands_on_intern_broker_only(temp_journal, tmp_path,
                                                 monkeypatch):
    monkeypatch.setattr(intern_trader, "INTERN_POSITIONS_FILE",
                        str(tmp_path / "ip.json"))
    broker = FakeInternBroker(price=100.0)
    line = intern_trader.execute_trade({"NVDA": make_verdict()}, broker=broker)
    assert "BOUGHT" in line
    assert len(broker.brackets) == 1
    ticker, qty, stop, target = broker.brackets[0]
    assert (ticker, stop) == ("NVDA", 95.0)            # HIS invalidation as stop
    assert target == pytest.approx(110.0)              # 2R
    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.row_factory = sqlite3.Row
    buys = conn.execute("SELECT * FROM trades WHERE action='BUY'").fetchall()
    conn.close()
    assert len(buys) == 1
    assert buys[0]["reason"].startswith("INTERN")
    assert buys[0]["broker_order_id"] == "int-br-1"


def test_one_entry_per_day_max(temp_journal, tmp_path, monkeypatch):
    monkeypatch.setattr(intern_trader, "INTERN_POSITIONS_FILE",
                        str(tmp_path / "ip.json"))
    broker = FakeInternBroker()
    intern_trader.execute_trade({"NVDA": make_verdict()}, broker=broker)
    line2 = intern_trader.execute_trade({"AMD": make_verdict()}, broker=broker)
    assert "already entered" in line2
    assert len(broker.brackets) == 1
