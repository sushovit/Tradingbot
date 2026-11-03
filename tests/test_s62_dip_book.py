"""CEO 2026-11-03: dip_probation is a SEPARATE book. Main entries ignore dip
positions for max_positions and the sector cap; the dip book keeps its own
limits (max_open 3, 1 entry a day, $25 risk, kill switch); the daily loss
limit counts both books."""

import json
import sqlite3
from types import SimpleNamespace as N

import pandas as pd

import dip_live
import universe
import tests.test_s19b_async_discord as harness
from tests.test_s19b_async_discord import _run_worker
from tests.test_s36_watch_only import FakeDaily

CFG = json.load(open("bot_config.json", encoding="utf-8"))


def _hold(setup):
    return {"in_position": True, "entry_price": 700.0, "shares_held": 1, "initial_stop": 680.0,
            "trailing_stop_price": 680.0, "profit_target_price": 760.0, "source": "bot",
            "timeframe": "intraday", "setup": setup, "hard_exit_date": "2026-10-16"}


def drive(app, monkeypatch, tmp_path, holds):
    """One worker cycle; `holds` = {ticker: setup} held locally and at the broker.
    A main signal fires on every ticker the worker scans for entry."""
    import analyst
    import daily_eval
    submitted = []
    monkeypatch.setattr(app, "enabled_strategies", lambda t, c: [FakeDaily()])
    monkeypatch.setattr(daily_eval, "gap_abort", lambda df, px: (False, 0, 0))
    monkeypatch.setattr(universe, "watch_only_today", lambda: {})
    monkeypatch.setattr(app, "earnings_gap_state_for", lambda *a, **k: ((None,) * 4, []))
    monkeypatch.setattr(app, "gatekeeper_event_context", lambda *a, **k: ("", []))
    monkeypatch.setattr(analyst, "get_verdict", lambda *a, **k: (
        {"approved": True, "conviction_score": 90, "reasoning": "test"}, None))

    def bars(self, tickers, timeframe_minutes=5, lookback_days=5):
        idx = pd.date_range("2026-10-14 13:30", periods=40, freq="5min", tz="UTC")
        px = [759.0 + 0.01 * i for i in range(40)]
        df = pd.DataFrame({"open": px, "high": px, "low": px, "close": px, "volume": 1000},
                          index=idx)
        return {t: df for t in tickers}
    monkeypatch.setattr(harness.WorkerBroker, "get_bars", bars)
    monkeypatch.setattr(harness.WorkerBroker, "get_latest_price", lambda self, t: 759.4,
                        raising=False)
    monkeypatch.setattr(harness.WorkerBroker, "get_positions", lambda self: [
        N(symbol=s, qty="1", avg_entry_price="700", current_price="759.4",
          unrealized_pl="59.4", market_value="759.4") for s in holds])

    def submit(self, *a, **k):
        submitted.append(a[0])
        return N(id=f"order-{a[0]}", legs=[])
    monkeypatch.setattr(harness.WorkerBroker, "submit_bracket", submit, raising=False)
    for name, fn in (("get_closed_orders_since", lambda self, *a, **k: []),
                     ("get_open_orders", lambda self, *a, **k: []),
                     ("orders_since", lambda self, *a, **k: []),
                     ("replace_stop", lambda self, *a, **k: N(id="stop-2")),
                     ("replace_target", lambda self, *a, **k: N(id="tgt-2"))):
        monkeypatch.setattr(harness.WorkerBroker, name, fn, raising=False)
    monkeypatch.setattr(harness.WorkerBroker, "get_order", lambda self, oid: N(
        id=oid, status="OrderStatus.FILLED", filled_avg_price=759.4, filled_qty="1",
        legs=[], symbol="X"), raising=False)
    (tmp_path / "positions.json").write_text(json.dumps({t: _hold(s) for t, s in holds.items()}))
    _run_worker(app, monkeypatch, tmp_path, cycles=1)
    return submitted


def blocked(temp_journal, reason):
    conn = sqlite3.connect(temp_journal.DB_FILE)
    rows = conn.execute("SELECT ticker FROM decisions WHERE source='rules' AND "
                        "(verdict LIKE ? OR context LIKE ?)", (f"%{reason}%",) * 2).fetchall()
    conn.close()
    return {r[0] for r in rows}


def test_four_open_dip_positions_do_not_block_a_main_entry(temp_journal, tmp_path, monkeypatch):
    import streamlit_app as app
    assert CFG["max_positions"] == 4
    dips = {t: "dip_probation" for t in ("ABBV", "PSX", "ZS", "KO")}
    sent = drive(app, monkeypatch, tmp_path, dips)
    assert "NVDA" in sent                                 # main book: 0 of 4 open
    assert not blocked(temp_journal, "max_positions")


def test_four_open_main_positions_still_block_a_main_entry(temp_journal, tmp_path, monkeypatch):
    import streamlit_app as app
    mains = {t: "momentum_continuation" for t in ("ABBV", "PSX", "ZS", "KO")}
    assert drive(app, monkeypatch, tmp_path, mains) == []
    assert "NVDA" in blocked(temp_journal, "max_positions")


def test_a_dip_position_does_not_sector_cap_a_main_entry(temp_journal, tmp_path, monkeypatch):
    import streamlit_app as app
    sent = drive(app, monkeypatch, tmp_path, {"AMD": "dip_probation"})   # same sector as NVDA
    assert "NVDA" in sent and "NVDA" not in blocked(temp_journal, "sector_cap")


def test_a_main_position_still_sector_caps_a_main_entry(temp_journal, tmp_path, monkeypatch):
    import streamlit_app as app
    assert "NVDA" not in drive(app, monkeypatch, tmp_path, {"AMD": "momentum_continuation"})
    assert "NVDA" in blocked(temp_journal, "sector_cap")


def test_dip_limits_are_unchanged_apart_from_max_open_3():
    p = dip_live.params(CFG)
    assert (p["max_open"], p["max_entries_per_day"], p["risk_per_trade_pct"],
            p["kill_pnl_usd"], p["kill_streak"]) == (3, 1, 0.5, -75.0, 5)
    two = {t: {"in_position": True, "setup": "dip_probation"} for t in ("A", "B")}
    three = dict(two, C={"in_position": True, "setup": "dip_probation"})
    import datetime as dt
    day = dt.date(2026, 11, 3)
    assert dip_live.cycle_gate(day, two, 0, [], CFG) is None
    assert dip_live.cycle_gate(day, three, 0, [], CFG) == "dip_position_open"
    assert dip_live.cycle_gate(day, two, 1, [], CFG) == "dip_entry_done_today"
    assert dip_live.presubmit_block(three, 0, [], CFG) == "dip_position_open"
    assert dip_live.presubmit_block(two, 1, [], CFG) == "dip_entry_done_today"
    assert dip_live.cycle_gate(day, {}, 0, [-40, -40], CFG).startswith("killed")


def test_the_daily_loss_limit_counts_both_books(temp_journal):
    import journal
    journal.log_trade("ZS", "SELL", 1, 207.0, pnl_usd=-25.0, reason="Stop Loss")      # dip book
    journal.log_trade("NVDA", "SELL", 1, 180.0, pnl_usd=-40.0, reason="Stop Loss")    # main book
    assert journal.daily_realized_pnl() == -65.0
    src = open("streamlit_app.py", encoding="utf-8").read()
    assert "daily_pnl = journal.daily_realized_pnl()" in src
    assert "breaker_tripped = daily_pnl <= -loss_limit_usd" in src


def test_the_worker_counts_the_books_separately():
    src = open("streamlit_app.py", encoding="utf-8").read()
    assert "open_positions_count = sum(1 for s in positions.values()\n" in src
    assert "if s.get(\"in_position\")) - _dip_open_n" in src
    assert "if not _is_dip_signal and open_positions_count >= max_positions:" in src
    assert src.count("open_positions=_book_open, max_positions=_book_max,") == 2
