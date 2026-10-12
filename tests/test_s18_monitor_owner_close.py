"""
S18 — position monitor, owner close, and owner closes in the report.

No network: the broker is a fake; the journal is the test's temp journal;
positions.json lives in tmp_path.
"""

import json
import threading
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest
import pytz

import journal as journal_mod
import orders
import owner_close
import position_monitor as pm
import report
import streamlit_app as app

ET = pytz.timezone("America/New_York")
WED = lambda h, m: ET.localize(datetime(2026, 10, 14, h, m))     # Wednesday
SAT = ET.localize(datetime(2026, 10, 17, 11, 0))


# ================================================== fakes

class Order(SimpleNamespace):
    pass


class FakeBroker:
    def __init__(self, held=None, legs=None, fill=None, bars=None):
        self.held = dict(held or {})               # symbol -> qty
        self.legs = {k: list(v) for k, v in (legs or {}).items()}
        self.fill = fill                           # (price, filled_at UTC)
        self.bars = bars or {}
        self.cancelled, self.closed, self.replaced = [], [], []
        self.orders = {}
        self.get_bars_calls = 0

    def get_positions(self):
        return [SimpleNamespace(symbol=s, qty=str(q)) for s, q in self.held.items()]

    def get_live_orders(self, ticker=None):
        if ticker is None:
            return [o for v in self.legs.values() for o in v]
        return list(self.legs.get(ticker, []))

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        for t in self.legs:
            self.legs[t] = [o for o in self.legs[t] if str(o.id) != oid]

    def close_position(self, ticker):
        self.closed.append((ticker, self.held.get(ticker)))
        order = Order(id="close-1", status="new", filled_avg_price=None,
                      filled_at=None)
        if self.fill:
            order = Order(id="close-1", status="OrderStatus.FILLED",
                          filled_avg_price=str(self.fill[0]),
                          filled_at=self.fill[1])
        self.orders["close-1"] = order
        return order

    def get_order(self, oid):
        return self.orders[str(oid)]

    def get_bars(self, tickers, timeframe_minutes=5, lookback_days=2):
        self.get_bars_calls += 1
        return {t: self.bars.get(t, pd.DataFrame()) for t in tickers}

    def replace_stop(self, order_id, price):
        self.replaced.append((order_id, price))
        return Order(id=f"stop-{len(self.replaced)}")


def bot_position(**over):
    state = {"in_position": True, "source": "bot", "timeframe": "daily",
             "setup": "mean_reversion_reclaim", "entry_price": 100.0,
             "initial_stop": 95.0, "trailing_stop_price": 95.0,
             "profit_target_price": 115.0, "stop_order_id": "stop-0",
             "target_order_id": "tgt-0", "shares_held": 3,
             "decision_id": None, "sector": "tech"}
    state.update(over)
    return state


CONFIG = {"interval": 5, "ticker_profiles": {},
          "risk_profiles": {"Moderate": {
              "trailing_stop_type": "ATR", "trailing_stop_value": 2.5,
              "daily_trailing": "profit_lock", "daily_profit_lock": [[1.0, 0.0]]}}}


def m5(closes, end=None):
    """Completed 5-minute bars ending well before `end` (start-stamped)."""
    end = end or pd.Timestamp("2026-10-14 15:00", tz="UTC")
    idx = pd.date_range(end=end - pd.Timedelta(minutes=10), periods=len(closes),
                        freq="5min")
    return pd.DataFrame({"open": closes, "high": closes, "low": closes,
                         "close": closes}, index=idx)


# ================================================== 1. monitor: timing

@pytest.mark.parametrize("now, expected", [
    (WED(9, 29), False), (WED(9, 30), True), (WED(15, 59), True),
    (WED(16, 0), False), (SAT, False)])
def test_monitor_runs_only_in_the_regular_session(now, expected):
    assert pm.in_monitor_window(now) is expected


@pytest.mark.parametrize("text, limited", [
    ("get_bars failed after 3 attempts: 429 Too Many Requests", True),
    ('{"code":42910000,"message":"rate limit exceeded"}', True),
    ("Connection aborted", False)])
def test_rate_limit_detection(text, limited):
    assert pm.is_rate_limited(Exception(text)) is limited


def test_backoff_doubles_to_a_cap_and_resets():
    b = pm.Backoff(start=60, cap=600)
    assert [b.hit() for _ in range(6)] == [60, 120, 240, 480, 600, 600]
    b.reset()
    assert b.hit() == 60


def test_run_monitor_backs_off_on_429_and_resets_after_success():
    calls, waits = iter(["429", "429", "ok", "stop"]), []

    class Broker429(FakeBroker):
        def get_bars(self, *a, **k):
            if next(calls) == "429":
                raise Exception("429 Too Many Requests")
            return {}
    positions = {"X": bot_position()}
    runs = iter([True, True, True, False])
    pm.run_monitor(Broker429(), positions, threading.RLock(),
                   manage=lambda *a: None, should_run=lambda: next(runs),
                   now_et_fn=lambda: WED(11, 0), sleep=waits.append,
                   interval=60)
    assert waits == [120, 180, 60]           # 60+60, 60+120, then reset


def test_run_monitor_does_nothing_outside_the_session():
    broker, waits = FakeBroker(), []
    runs = iter([True, False])
    pm.run_monitor(broker, {"X": bot_position()}, threading.RLock(),
                   manage=lambda *a: pytest.fail("must not manage"),
                   should_run=lambda: next(runs), now_et_fn=lambda: SAT,
                   sleep=waits.append)
    assert broker.get_bars_calls == 0 and waits == [60]


# ================================================== 1. monitor: what it manages

class RecordingLock:
    def __init__(self):
        self.inner, self.held = threading.RLock(), 0

    def __enter__(self):
        self.inner.acquire()
        self.held += 1

    def __exit__(self, *exc):
        self.held -= 1
        self.inner.release()


def test_tick_manages_only_open_bot_positions_under_the_lock():
    lock, seen = RecordingLock(), []
    positions = {"A": bot_position(), "B": bot_position(source="ceo"),
                 "C": {"in_position": False}}
    broker = FakeBroker(held={"A": 3, "B": 1}, bars={"A": m5([100.0])})

    def manage(b, p, ticker, df, symbols, now):
        seen.append((ticker, lock.held > 0, symbols))
    assert pm.monitor_tick(broker, positions, lock, WED(11, 0), manage) == 1
    assert seen == [("A", True, {"A", "B"})]


def test_tick_skips_a_position_exited_since_the_pass_began():
    positions = {"A": bot_position()}
    broker = FakeBroker(held={"A": 3}, bars={"A": m5([100.0])})

    class ExitingLock(RecordingLock):
        def __enter__(self):
            super().__enter__()
            if self.held == 1 and getattr(self, "entered", False):
                positions["A"] = {"in_position": False}   # main cycle won
            self.entered = True
    pm.monitor_tick(broker, positions, ExitingLock(), WED(11, 0),
                    manage=lambda *a: pytest.fail("already exited"))


def test_tick_with_nothing_open_makes_no_broker_calls():
    broker = FakeBroker()
    assert pm.monitor_tick(broker, {}, threading.RLock(), WED(11, 0),
                           manage=lambda *a: None) == 0
    assert broker.get_bars_calls == 0


@pytest.fixture
def desk(tmp_path, monkeypatch, temp_journal):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot_config.json").write_text(json.dumps(CONFIG))
    return tmp_path


def test_monitor_raises_the_profit_lock_and_persists(desk):
    positions = {"A": bot_position()}
    broker = FakeBroker(held={"A": 3}, bars={"A": m5([101.0, 105.2])})
    broker.orders = {"stop-0": Order(status="new"), "tgt-0": Order(status="new")}
    app.monitor_manage_position(broker, positions, "A", broker.bars["A"],
                                {"A"}, WED(11, 0), CONFIG)
    assert broker.replaced == [("stop-0", 100.0)]          # BE at +1R
    assert positions["A"]["trailing_stop_price"] == 100.0
    saved = json.loads((desk / "positions.json").read_text())
    assert saved["A"]["trailing_stop_price"] == 100.0
    rows = journal_mod._connect().execute(
        "SELECT action, price FROM trades").fetchall()
    assert [tuple(r) for r in rows] == [("TIGHTEN_STOP", 100.0)]


def test_monitor_never_lowers_a_stop(desk):
    positions = {"A": bot_position(trailing_stop_price=102.0)}
    broker = FakeBroker(held={"A": 3}, bars={"A": m5([105.2])})
    broker.orders = {"stop-0": Order(status="new"), "tgt-0": Order(status="new")}
    app.monitor_manage_position(broker, positions, "A", broker.bars["A"],
                                {"A"}, WED(11, 0), CONFIG)
    assert broker.replaced == [] and positions["A"]["trailing_stop_price"] == 102.0


def test_monitor_detects_a_filled_stop_and_journals_the_exit(desk, monkeypatch):
    monkeypatch.setattr(app, "send_discord_notification", lambda *a, **k: None)
    positions = {"A": bot_position()}
    broker = FakeBroker(held={}, bars={"A": m5([94.0])})
    broker.orders = {"stop-0": Order(status="filled", filled_avg_price="94.9"),
                     "tgt-0": Order(status="canceled")}
    status = app.monitor_manage_position(broker, positions, "A",
                                         broker.bars["A"], set(), WED(11, 0),
                                         CONFIG)
    assert status == "A: SOLD (Stop Loss)"
    assert positions["A"] == {"in_position": False}
    rows = journal_mod._connect().execute(
        "SELECT action, price, broker_order_id FROM trades").fetchall()
    assert [tuple(r) for r in rows] == [("SELL", 94.9, "stop-0")]


def test_monitor_and_main_cycle_cannot_double_handle_an_exit(desk, monkeypatch):
    """Two threads race to the same filled stop under the shared lock: one
    SELL row, state cleared once."""
    monkeypatch.setattr(app, "send_discord_notification", lambda *a, **k: None)
    positions = {"A": bot_position()}
    broker = FakeBroker(held={})
    broker.orders = {"stop-0": Order(status="filled", filled_avg_price="94.9"),
                     "tgt-0": Order(status="canceled")}
    results = []

    def contender():
        with app.POSITIONS_LOCK:
            state = positions.get("A", {})
            if state.get("in_position"):
                results.append(app.detect_and_handle_exit(
                    broker, positions, "A", state, set(), WED(11, 0)))
    threads = [threading.Thread(target=contender) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == ["A: SOLD (Stop Loss)"]
    assert journal_mod._connect().execute(
        "SELECT COUNT(*) FROM trades WHERE action='SELL'").fetchone()[0] == 1


def test_the_worker_starts_the_monitor_and_stops_it_with_the_loop():
    with open("streamlit_app.py", encoding="utf-8") as f:
        src = f.read()
    start = src[src.index("position_monitor.start_monitor_thread("):][:600]
    assert "lock=POSITIONS_LOCK" in start
    assert "manage=monitor_manage_position" in start
    assert "os.path.exists(LOCK_FILE)" in start
    assert "finally:\n        monitor_stop.set()" in src
    # The main cycle's position block holds the same lock and re-reads state.
    block = src[src.index("# =================== MANAGE OPEN POSITION"):][:900]
    assert "with POSITIONS_LOCK:" in block
    assert "state = positions.get(ticker, {})" in block


def test_write_positions_holds_the_lock():
    import inspect
    assert "with POSITIONS_LOCK:" in inspect.getsource(app.write_positions)


# ================================================== 2. owner close

FILLED_AT = pd.Timestamp("2026-10-14 15:31:07", tz="UTC")     # 11:31:07 ET


@pytest.fixture
def owner_desk(tmp_path, monkeypatch, temp_journal):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot_config.json").write_text(json.dumps(CONFIG))

    def write(positions):
        (tmp_path / "positions.json").write_text(json.dumps(positions))
    return write


def legs():
    return {"A": [Order(id="stop-0"), Order(id="tgt-0")]}


def close(broker):
    return owner_close.close_bot_position("A", broker, sleep=lambda s: None,
                                          out=lambda *a: None)


def test_refuses_a_ticker_that_is_not_open(owner_desk):
    owner_desk({"A": {"in_position": False}})
    broker = FakeBroker(held={"A": 3}, legs=legs(), fill=(104.0, FILLED_AT))
    with pytest.raises(owner_close.OwnerCloseRefused, match="not an open"):
        close(broker)
    assert broker.closed == [] and broker.cancelled == []


@pytest.mark.parametrize("source", ["ceo", "unknown", None])
def test_refuses_a_position_that_is_not_the_bots(owner_desk, source):
    owner_desk({"A": bot_position(source=source)})
    broker = FakeBroker(held={"A": 3}, legs=legs(), fill=(104.0, FILLED_AT))
    with pytest.raises(owner_close.OwnerCloseRefused, match="not a bot"):
        close(broker)
    assert broker.closed == []


def test_refuses_when_the_broker_does_not_hold_it(owner_desk):
    owner_desk({"A": bot_position()})
    broker = FakeBroker(held={}, legs=legs(), fill=(104.0, FILLED_AT))
    with pytest.raises(owner_close.OwnerCloseRefused, match="not held"):
        close(broker)
    assert broker.closed == []


def test_does_not_sell_while_a_leg_is_still_live(owner_desk):
    owner_desk({"A": bot_position()})

    class StickyLegs(FakeBroker):
        def cancel_order(self, oid):
            self.cancelled.append(oid)            # never actually goes away
    broker = StickyLegs(held={"A": 3}, legs=legs(), fill=(104.0, FILLED_AT))
    with pytest.raises(owner_close.OwnerCloseRefused, match="still live"):
        close(broker)
    assert broker.closed == []


def test_owner_close_sells_full_qty_and_journals_owner_close(owner_desk, tmp_path):
    owner_desk({"A": bot_position(trailing_stop_price=100.0)})
    broker = FakeBroker(held={"A": 3}, legs=legs(), fill=(104.0, FILLED_AT))
    result = close(broker)
    assert sorted(broker.cancelled) == ["stop-0", "tgt-0"]
    assert broker.closed == [("A", 3)]                       # full broker qty
    row = journal_mod._connect().execute(
        "SELECT action, qty, price, reason, timestamp, broker_order_id, "
        "pnl_usd FROM trades").fetchone()
    assert tuple(row) == ("SELL", 3.0, 104.0, "Owner close",
                          "2026-10-14 11:31:07", "close-1", 12.0)
    assert result["trade_id"] is not None and result["pnl"] == 12.0
    saved = json.loads((tmp_path / "positions.json").read_text())
    assert saved["A"] == {"in_position": False}
    ctx = journal_mod.owner_closes()[0]
    assert (ctx["stop"], ctx["target"], ctx["original_stop"]) == (100.0, 115.0, 95.0)
    assert ctx["profit_lock"] == [[1.0, 0.0]]


def test_a_worker_journaling_the_fill_first_still_ends_as_owner_close(owner_desk):
    """The running worker may see the position gone and journal the fill as
    "(resolved)" before the command does: one row, renamed, real fill time."""
    owner_desk({"A": bot_position()})
    journal_mod.record_exit("A", 3, 104.0, "Exit (resolved)",
                            broker_order_id="close-1", entry_price=100.0)
    broker = FakeBroker(held={"A": 3}, legs=legs(), fill=(104.0, FILLED_AT))
    close(broker)
    rows = journal_mod._connect().execute(
        "SELECT reason, timestamp FROM trades WHERE action='SELL'").fetchall()
    assert [tuple(r) for r in rows] == [("Owner close", "2026-10-14 11:31:07")]


def test_an_unconfirmed_fill_journals_nothing(owner_desk):
    owner_desk({"A": bot_position()})
    broker = FakeBroker(held={"A": 3}, legs=legs(), fill=None)
    with pytest.raises(owner_close.OwnerCloseRefused, match="not filled"):
        close(broker)
    assert journal_mod._connect().execute(
        "SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def test_the_cli_exits_one_when_refused(owner_desk, capsys):
    owner_desk({})
    assert orders.owner_close_cli("A", broker=FakeBroker()) == 1
    assert "NOT CLOSED" in capsys.readouterr().out


def test_the_cli_exits_zero_on_a_close(owner_desk):
    owner_desk({"A": bot_position()})
    broker = FakeBroker(held={"A": 3}, legs=legs(), fill=(104.0, FILLED_AT))
    assert orders.owner_close_cli("A", broker=broker) == 0
    assert broker.closed == [("A", 3)]


# ================================================== 3. what the rule would have done

def bars_from(start_et, rows):
    idx = pd.date_range(start=start_et, periods=len(rows), freq="5min")
    o, h, l, c = zip(*rows)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c},
                        index=idx)


CLOSE_ET = pd.Timestamp("2026-10-14 11:31:07", tz="America/New_York")


def test_next_session_close():
    assert owner_close.next_session_close(CLOSE_ET) == \
        pd.Timestamp("2026-10-14 16:00", tz="America/New_York")
    fri_late = pd.Timestamp("2026-10-16 16:30", tz="America/New_York")
    assert owner_close.next_session_close(fri_late) == \
        pd.Timestamp("2026-10-19 16:00", tz="America/New_York")


def test_rule_outcome_stop_target_hold_and_lock():
    start = pd.Timestamp("2026-10-14 11:35", tz="America/New_York")
    flat = [(104, 104.5, 103.5, 104)] * 60          # to 16:35, past the close
    px, how = owner_close.rule_outcome(bars_from(start, flat), CLOSE_ET,
                                       100.0, 95.0, 115.0, 95.0, [])
    assert (px, how) == (104.0, "held to the session close")
    stop = [(104, 104, 94, 95)] + flat
    assert owner_close.rule_outcome(bars_from(start, stop), CLOSE_ET, 100.0,
                                    95.0, 115.0, 95.0, []) == (95.0, "stop")
    target = [(104, 116, 103, 115)] + flat
    assert owner_close.rule_outcome(bars_from(start, target), CLOSE_ET, 100.0,
                                    95.0, 115.0, 95.0, []) == (115.0, "target")
    # +1R close raises the lock to BE; a later dip exits at entry, not 95.
    lock = [(104, 106, 104, 105.5), (105, 105, 99, 99.5)] + flat
    assert owner_close.rule_outcome(bars_from(start, lock), CLOSE_ET, 100.0,
                                    95.0, 115.0, 95.0, [[1.0, 0.0]]) == \
        (100.0, "stop")


def test_rule_outcome_is_pending_before_the_session_close():
    start = pd.Timestamp("2026-10-14 11:35", tz="America/New_York")
    short = bars_from(start, [(104, 104.5, 103.5, 104)] * 10)
    assert owner_close.rule_outcome(short, CLOSE_ET, 100.0, 95.0, 115.0,
                                    95.0, []) == (None, "pending")


def test_report_counts_owner_closes_separately(owner_desk):
    owner_desk({"A": bot_position()})
    broker = FakeBroker(held={"A": 3}, legs=legs(), fill=(104.0, FILLED_AT))
    close(broker)
    start = pd.Timestamp("2026-10-14 11:35", tz="America/New_York")
    broker.bars = {"A": bars_from(start, [(104, 109, 103.5, 108)] * 60)}
    lines = report.owner_close_section(
        broker, now_et=pd.Timestamp("2026-10-15 09:00", tz="America/New_York"))
    text = "\n".join(lines)
    assert "## Owner closes" in text
    assert "**1 owner close(s), total P&L $+12.00.**" in text
    # Rule: held to 16:00 at 108 -> +24.00; owner vs rule -12.00.
    assert "would have made $+24.00 on the 1 that can be compared" in text
    assert "owner vs rule $-12.00" in text


def test_report_with_no_owner_closes(temp_journal):
    assert report.owner_close_section(FakeBroker())[-1] == "_None._"


def test_report_build_includes_the_section():
    import inspect
    assert "owner_close_section(broker)" in inspect.getsource(report.build_report)
