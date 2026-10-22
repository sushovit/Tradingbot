"""Standing rule (CEO 2026-10-21, agenda 24): NEVER AVERAGE DOWN - the worker
never buys a symbol it already holds, whether its own record knows about the
position or only the broker does."""

import json
import sqlite3

import pandas as pd

import universe
from tests.test_s36_watch_only import FakeDaily
from tests.test_s19b_async_discord import _run_worker
import tests.test_s19b_async_discord as harness
from types import SimpleNamespace as N


def drive(app, monkeypatch, tmp_path, broker_holds=(), local_holds=()):
    import analyst
    import daily_eval
    asked, submitted = [], []
    monkeypatch.setattr(app, "enabled_strategies", lambda t, c: [FakeDaily()])
    monkeypatch.setattr(daily_eval, "gap_abort", lambda df, px: (False, 0, 0))
    monkeypatch.setattr(universe, "watch_only_today", lambda: {})

    def verdict(mode, ticker, setup, ctx, kw):
        asked.append(ticker)
        return {"approved": True, "conviction_score": 90, "reasoning": "test"}, None
    monkeypatch.setattr(analyst, "get_verdict", verdict)

    def bars(self, tickers, timeframe_minutes=5, lookback_days=5):
        idx = pd.date_range("2026-10-14 13:30", periods=40, freq="5min", tz="UTC")
        px = [759.0 + 0.01 * i for i in range(40)]
        df = pd.DataFrame({"open": px, "high": px, "low": px, "close": px,
                           "volume": 1000}, index=idx)
        return {t: df for t in tickers}
    monkeypatch.setattr(harness.WorkerBroker, "get_bars", bars)
    monkeypatch.setattr(harness.WorkerBroker, "get_latest_price",
                        lambda self, t: 759.4, raising=False)
    monkeypatch.setattr(harness.WorkerBroker, "get_positions", lambda self: [
        N(symbol=s, qty="1", avg_entry_price="700", current_price="759.4",
          unrealized_pl="59.4", market_value="759.4") for s in broker_holds])
    def submit(self, *a, **k):
        submitted.append(a)
        return N(id=f"order-{a[0] if a else 'x'}")
    monkeypatch.setattr(harness.WorkerBroker, "submit_bracket", submit, raising=False)
    for name, fn in (("get_closed_orders_since", lambda self, *a, **k: []),
                     ("get_open_orders", lambda self, *a, **k: []),
                     ("replace_stop", lambda self, *a, **k: N(id="stop-2")),
                     ("replace_target", lambda self, *a, **k: N(id="tgt-2"))):
        monkeypatch.setattr(harness.WorkerBroker, name, fn, raising=False)
    monkeypatch.setattr(harness.WorkerBroker, "get_order", lambda self, oid: N(
        id=oid, status="OrderStatus.FILLED", filled_avg_price=759.4,
        filled_qty="1", legs=[], symbol="X"), raising=False)
    if local_holds:
        (tmp_path / "positions.json").write_text(json.dumps({
            s: {"in_position": True, "entry_price": 700.0, "shares_held": 1,
                "initial_stop": 680.0, "trailing_stop_price": 680.0,
                "profit_target_price": 760.0, "source": "bot",
                "timeframe": "intraday"} for s in local_holds}))
    _run_worker(app, monkeypatch, tmp_path, cycles=2)
    return asked, submitted


def blocked(temp_journal, reason):
    conn = sqlite3.connect(temp_journal.DB_FILE)
    rows = conn.execute("SELECT ticker FROM decisions WHERE source='rules' AND "
                        "(verdict LIKE ? OR context LIKE ?)", (f"%{reason}%",) * 2).fetchall()
    conn.close()
    return {r[0] for r in rows}


def test_a_symbol_held_only_at_the_broker_is_never_bought(temp_journal, tmp_path,
                                                         monkeypatch):
    """Stale local record: the broker-side duplicate check still refuses."""
    import streamlit_app as app
    asked, submitted = drive(app, monkeypatch, tmp_path, broker_holds=("NVDA",))
    assert "NVDA" in asked and "AMD" in asked        # both reached the gate...
    assert not [a for a in submitted if a and a[0] == "NVDA"]   # ...NVDA refused
    assert "NVDA" in blocked(temp_journal, "duplicate_entry_blocked")


def test_a_symbol_the_worker_holds_is_managed_never_re_entered(temp_journal, tmp_path,
                                                              monkeypatch):
    import streamlit_app as app
    asked, submitted = drive(app, monkeypatch, tmp_path, broker_holds=("NVDA",),
                             local_holds=("NVDA",))
    assert "NVDA" not in asked                       # NVDA never scanned for entry
    # (AMD shares NVDA's sector, so since 2026-10-22 the sector cap stops it
    # before the gatekeeper - it is journaled, which shows the gates ran)
    assert "AMD" in blocked(temp_journal, "sector_cap")
    assert not [a for a in submitted if a and a[0] == "NVDA"]


def test_the_manage_branch_always_ends_before_the_entry_scan():
    src = open("streamlit_app.py", encoding="utf-8").read()
    manage = src.index("# =================== MANAGE OPEN POSITION")
    scan = src.index("# =================== SCAN FOR NEW ENTRY")
    branch = src[manage:scan].rstrip().splitlines()
    assert branch[-1].strip() == "continue"
    assert src.index("duplicate_entry_exists(broker, ticker)") > scan
    assert src.index("duplicate_entry_exists(broker, ticker)") < src.index("submit_bracket(", scan)
