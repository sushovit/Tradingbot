"""
S19b — Discord never blocks. Every test here runs with Discord HUNG: the
sender blocks for 60 s (released at teardown so no thread outlives the
test), and the trading/order path must still finish in normal time.
"""

import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone

import pandas as pd
import pytest

import alerts
import orders

pytestmark = pytest.mark.real_post_async   # tests post_async itself

ROOT = os.path.join(os.path.dirname(__file__), "..")
FAST = 5.0            # seconds: "normal time" for anything below


@pytest.fixture
def hung_discord(monkeypatch):
    """Every Discord send blocks up to 60 s. Records what was sent."""
    release = threading.Event()
    sent = []

    def hang(payload, url=None):
        sent.append(payload)
        release.wait(60)

    monkeypatch.setattr(alerts, "_send", hang)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/fake")
    yield sent
    release.set()
    alerts.drain(5)


def _wait_for(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


# ================================================== the helper

def test_post_async_returns_at_once_while_discord_hangs(hung_discord):
    start = time.monotonic()
    t = alerts.post_async("hello")
    assert time.monotonic() - start < 0.5
    assert t is not None and t.daemon
    assert _wait_for(lambda: hung_discord == ["hello"])


def test_post_async_never_raises_and_logs_redacted(caplog):
    def boom(payload, url=None):
        raise RuntimeError("POST https://discord.com/api/webhooks/1/SECRETtok3n failed")
    t = alerts.post_async("x", sender=boom)
    t.join(2)
    assert "SECRETtok3n" not in caplog.text and "<redacted>" in caplog.text
    assert alerts.post_async(lambda: 1 / 0).join(2) is None    # builder errors too


def test_payload_builder_runs_off_the_caller_thread():
    seen = []
    t = alerts.post_async(lambda: seen.append(threading.current_thread().name),
                          sender=lambda p, u: None)
    t.join(2)
    assert seen == ["discord-alert"]


def test_drain_waits_at_exit_but_is_bounded(hung_discord):
    alerts.post_async("slow")
    start = time.monotonic()
    still_running = alerts.drain(0.3)
    assert time.monotonic() - start < 1.5 and still_running >= 1


def test_no_caller_can_join_any_more():
    """`wait=` is gone from every helper and call site."""
    for name in ("trade_alerts.py", "orders.py", "streamlit_app.py"):
        src = open(os.path.join(ROOT, name), encoding="utf-8").read()
        assert "wait=True" not in src, name
        # a thread join, not str.join: t.join(...) / .join(timeout=...)
        assert not re.search(r"\b(t|thread)\.join\(|\.join\(timeout", src), name
    src = open(os.path.join(ROOT, "streamlit_app.py"), encoding="utf-8").read()
    start = src.index("def send_discord_notification(")
    body = src[start:src.index("\ndef ", start + 10)]
    assert "alerts.post_async(" in body and ".execute(" not in body


# ================================================== order paths

class Leg:
    def __init__(self, id, order_type):
        self.id, self.order_type = id, order_type


class Bracket:
    def __init__(self, id="br-1", fill=None):
        self.id = id
        self.status = "filled" if fill else "new"
        self.filled_avg_price = fill
        self.legs = [Leg(f"{id}-stop", "stop"), Leg(f"{id}-limit", "limit")]


class ExecBroker:
    def get_positions(self):
        return []

    def get_live_orders(self, ticker=None):
        return []

    def get_latest_price(self, ticker):
        return 205.3

    def submit_bracket(self, ticker, qty, stop, target):
        return Bracket(f"br-{ticker}")

    def get_order(self, order_id):
        return Bracket(order_id, fill=164.21)

    def close_position(self, ticker):
        return type("O", (), {"id": f"close-{ticker}"})()


def test_ceo_buy_completes_in_normal_time_and_alerts(hung_discord, temp_journal,
                                                    tmp_path, monkeypatch):
    monkeypatch.setattr(orders, "POSITIONS_STATE_FILE", str(tmp_path / "p.json"))
    order = {"action": "BUY", "ticker": "SPCX", "notional_usd": 250,
             "entry": 165.40, "stop": 158.0, "target": 178.0,
             "setup": "event_flow", "hard_exit_date": "2026-11-01"}
    start = time.monotonic()
    result = orders.execute_order(order, ExecBroker(), None, {})
    assert time.monotonic() - start < FAST and "BUY SPCX" in result
    assert _wait_for(lambda: any("BUY SPCX" in p["title"] for p in hung_discord))


def test_ceo_close_completes_in_normal_time_and_alerts(hung_discord, temp_journal):
    did = temp_journal.log_decision("PLTR", "ceo", {"stop": 195.0}, {})
    temp_journal.log_trade("PLTR", "BUY", 2, 202.8, reason="CEO x",
                           decision_id=did)
    positions = {"PLTR": {"in_position": True, "entry_price": 202.8,
                          "shares_held": 2, "decision_id": did}}
    start = time.monotonic()
    result = orders.execute_order({"action": "CLOSE", "ticker": "PLTR"},
                                  ExecBroker(), did, positions)
    assert time.monotonic() - start < FAST and "closed" in result
    assert _wait_for(lambda: any("SELL PLTR" in p["title"] for p in hung_discord))


class ClosedSell:
    def __init__(self):
        self.id, self.symbol, self.side = "sell-1", "ON", "sell"
        self.filled_qty, self.filled_avg_price = 5, 82.81
        self.filled_at = datetime.now(timezone.utc)
        self.order_type = "stop"


class SyncBroker:
    def get_closed_orders_since(self, since_utc):
        return [ClosedSell()]


def test_sync_journals_an_exit_in_normal_time_and_alerts(hung_discord, temp_journal,
                                                         tmp_path, monkeypatch):
    monkeypatch.setattr(orders, "POSITIONS_STATE_FILE", str(tmp_path / "p.json"))
    did = temp_journal.log_decision("ON", "momentum_continuation", {}, {})
    temp_journal.log_trade("ON", "BUY", 5, 84.75, decision_id=did)
    start = time.monotonic()
    orders.sync(broker=SyncBroker())
    assert time.monotonic() - start < FAST
    assert _wait_for(lambda: any("SELL ON" in p["title"] for p in hung_discord))


# ================================================== a full worker cycle

class WorkerBroker:
    def get_equity(self):
        return 5000.0

    def get_positions(self):
        return []

    def get_live_orders(self, ticker=None):
        return []

    def get_daily_bars(self, tickers, lookback_days=60):
        idx = pd.bdate_range(end="2026-10-13", periods=60, tz="UTC")
        closes = [700.0 + i for i in range(60)]        # rising: trending
        spy = pd.DataFrame({"open": closes, "high": closes, "low": closes,
                            "close": closes, "volume": 1}, index=idx)
        return {t: spy for t in tickers}

    def get_bars(self, tickers, timeframe_minutes=5, lookback_days=5):
        return {}


def _run_worker(app, monkeypatch, tmp_path, cycles):
    """One worker start, `cycles` complete cycles, Wednesday 10:00 ET."""
    import claude_integration
    import local_analyst
    import session_clock
    import universe

    shutil.copy(os.path.join(ROOT, "bot_config.json"), tmp_path / "bot_config.json")
    monkeypatch.chdir(tmp_path)
    open(app.LOCK_FILE, "w").write(str(os.getpid()))

    class Wed10(datetime):
        @classmethod
        def now(cls, tz=None):
            return app.EASTERN_TZ.localize(datetime(2026, 10, 14, 10, 0))

    done, statuses = [], []
    monkeypatch.setattr(app, "write_status", lambda msg, *a, **k: statuses.append(msg))

    def sleep(_secs):
        done.append(1)
        if len(done) >= cycles and os.path.exists(app.LOCK_FILE):
            os.remove(app.LOCK_FILE)

    monkeypatch.setattr(app, "Broker", WorkerBroker)
    monkeypatch.setattr(app, "datetime", Wed10)
    monkeypatch.setattr(app.a_time, "sleep", sleep)
    monkeypatch.setattr(app, "reconcile_positions", lambda b, p: p)
    monkeypatch.setattr(app, "refresh_order_ids", lambda b, p: [])
    monkeypatch.setattr(universe, "refresh", lambda *a, **k: ["NVDA", "AMD"])
    monkeypatch.setattr(universe, "load_universe_tickers", lambda *a, **k: [])
    monkeypatch.setattr(local_analyst, "ensure_ollama", lambda: {
        "up": True, "detail": "stub", "started": False, "warmed": True})
    monkeypatch.setattr(claude_integration, "credit_preflight",
                        lambda: {"ok": True})
    monkeypatch.setattr(session_clock, "keep_awake", lambda on: True)
    monkeypatch.setattr(session_clock, "suspend_gap", lambda *a, **k: None)
    start = time.monotonic()
    app._worker_loop()
    return time.monotonic() - start, [m for m in statuses if m.startswith("Cycle #")]


def test_full_cycles_run_in_normal_time_with_discord_hung(hung_discord, temp_journal,
                                                          tmp_path, monkeypatch):
    import streamlit_app as app
    elapsed, cycles = _run_worker(app, monkeypatch, tmp_path, cycles=3)
    # three COMPLETED cycles (the status line is the cycle's last act)
    assert [c.split(" |")[0] for c in cycles] == ["Cycle #1", "Cycle #2", "Cycle #3"]
    assert elapsed < FAST
    assert _wait_for(lambda: len(hung_discord) >= 1)
    ups = [p for p in hung_discord if "Desk up" in p["title"]]
    assert len(ups) == 1                       # once per start, not per cycle
    f = dict(ups[0]["fields"])
    assert f["First cycle"] == "#1 completed" and f["Capital"] == "$5,000.00"
    assert f["Regime"] == "trending" and f["Universe"] == "2 names"


def test_desk_up_posts_once_per_worker_start(hung_discord, temp_journal,
                                             tmp_path, monkeypatch):
    import streamlit_app as app
    for _ in range(2):                          # two worker starts
        _run_worker(app, monkeypatch, tmp_path, cycles=2)
    assert _wait_for(lambda: sum("Desk up" in p["title"] for p in hung_discord) == 2)
    time.sleep(0.2)
    assert sum("Desk up" in p["title"] for p in hung_discord) == 2


def test_no_broker_read_for_desk_up_before_the_loop():
    src = open(os.path.join(ROOT, "streamlit_app.py"), encoding="utf-8").read()
    loop = src.index("def _worker_loop():")
    first_cycle = src.index("    while os.path.exists(LOCK_FILE):\n        cycle_count", loop)
    assert "alert_startup" not in src[loop:first_cycle]
    assert "alert_startup(" in src[first_cycle:src.index("\ndef ", first_cycle)]


def test_alerts_send_uses_a_10s_timeout(monkeypatch):
    seen = {}

    class WH:
        def __init__(self, url, **kw):
            seen.update(kw)

        def add_embed(self, e):
            pass

        def execute(self):
            return type("R", (), {"status_code": 200})()
    import net
    monkeypatch.setattr(net, "webhook", lambda url, **kw: WH(url, **kw))
    alerts._send("hi", url="https://discord.com/api/webhooks/1/fake")
    assert seen["timeout"] == 10
    alerts._send({"title": "t", "color": 1, "fields": [("a", "b")]},
                 url="https://discord.com/api/webhooks/1/fake")
    assert seen["timeout"] == 10
