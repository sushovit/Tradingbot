"""S36 (CEO 2026-10-21): catalyst names are WATCH-ONLY. A setup that fires
on one is journaled (rules, watch_only_catalyst, with its geometry so the
outcome tracker grades it) and never reaches the gatekeeper or an order;
every other name is unaffected."""

import json
import sqlite3
from datetime import datetime

import pandas as pd

import universe
from strategies.base import Signal
from tests.test_s19b_async_discord import _run_worker

ET = universe.EASTERN_TZ


class FakeDaily:
    name = "fake_intraday_setup"
    timeframe = "intraday"

    def detect(self, df, context):
        e = float(df["close"].iloc[-1])
        return Signal(setup_name=self.name, ticker=context["ticker"], entry=e,
                      stop=e * 0.97, target=e * 1.09, confidence_hint="medium",
                      reasoning="fake", extras={})


def run(app, monkeypatch, tmp_path, watch):
    import analyst
    import daily_eval
    asked, submitted = [], []
    monkeypatch.setattr(app, "enabled_strategies", lambda t, c: [FakeDaily()])
    monkeypatch.setattr(daily_eval, "gap_abort", lambda df, px: (False, 0, 0))
    monkeypatch.setattr(universe, "watch_only_today", lambda: watch)

    def verdict(mode, ticker, setup, ctx, kw):
        asked.append(ticker)
        return {"approved": False, "conviction_score": 10,
                "rejection_reason": "test"}, None
    monkeypatch.setattr(analyst, "get_verdict", verdict)
    import tests.test_s19b_async_discord as harness
    monkeypatch.setattr(harness.WorkerBroker, "submit_bracket",
                        lambda self, *a, **k: submitted.append(a), raising=False)

    def bars(self, tickers, timeframe_minutes=5, lookback_days=5):
        idx = pd.date_range("2026-10-14 13:30", periods=40, freq="5min", tz="UTC")
        px = [759.0 + 0.01 * i for i in range(40)]
        df = pd.DataFrame({"open": px, "high": px, "low": px, "close": px,
                           "volume": 1000}, index=idx)
        return {t: df for t in tickers}
    monkeypatch.setattr(harness.WorkerBroker, "get_bars", bars)
    monkeypatch.setattr(harness.WorkerBroker, "get_latest_price",
                        lambda self, t: 759.4, raising=False)
    _run_worker(app, monkeypatch, tmp_path, cycles=2)
    return asked, submitted


def rules_rows(temp_journal, reason):
    conn = sqlite3.connect(temp_journal.DB_FILE)
    rows = conn.execute("SELECT ticker, context FROM decisions WHERE source='rules' "
                        "AND (verdict LIKE ? OR context LIKE ?)", (f"%{reason}%",) * 2).fetchall()
    conn.close()
    return rows


def test_a_catalyst_name_is_journaled_never_ordered(temp_journal, tmp_path,
                                                     monkeypatch, caplog):
    import logging
    import streamlit_app as app
    with caplog.at_level(logging.INFO):
        asked, submitted = run(app, monkeypatch, tmp_path,
                               {"NVDA": {"gap_pct": 6.2, "tag": "earnings"}})
    assert "NVDA" not in asked and not [a for a in submitted if a and a[0] == "NVDA"]
    assert "AMD" in asked                         # non-catalyst unaffected
    rows = rules_rows(temp_journal, "watch_only_catalyst")
    assert [r[0] for r in rows] == ["NVDA"]       # once, not once per cycle
    ctx = json.loads(rows[0][1])
    assert "entry=" in ctx["details"] and "stop=" in ctx["details"]
    import outcomes
    assert outcomes.geometry(rows[0][1]) is not None      # gradeable
    assert "NVDA: watch_only: catalyst" in caplog.text


def test_without_catalysts_every_name_reaches_the_gatekeeper(temp_journal, tmp_path,
                                                             monkeypatch):
    import streamlit_app as app
    asked, _ = run(app, monkeypatch, tmp_path, {})
    assert {"NVDA", "AMD"} <= set(asked)
    assert rules_rows(temp_journal, "watch_only_catalyst") == []


def test_universe_rows_are_tagged_catalyst_and_watch_only():
    out = universe.merge_catalysts(
        [{"symbol": "AAPL", "source": "movers"}],
        [{"symbol": "AAPL", "gap_pct": 3.4, "tag": "earnings"},
         {"symbol": "NEWC", "gap_pct": -5.0, "tag": "M&A"}])
    by = {c["symbol"]: c for c in out}
    assert by["AAPL"]["catalyst"] and by["AAPL"]["watch_only"]
    assert by["AAPL"]["source"] == "movers"                    # row kept
    assert by["NEWC"]["catalyst"] and by["NEWC"]["watch_only"]


def test_watch_only_reads_todays_universe_and_catalysts_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    today = datetime.now(ET).strftime("%Y-%m-%d")
    (tmp_path / "universe_today.json").write_text(json.dumps({"date": today, "candidates": [
        {"symbol": "AAPL", "watch_only": True, "catalyst_gap_pct": 4.0},
        {"symbol": "MSFT"}]}))
    (tmp_path / "catalysts_today.json").write_text(json.dumps({"date": today, "names": [
        {"symbol": "NEWC", "gap_pct": 7.0, "tag": "FDA-clinical"}]}))
    assert set(universe.watch_only_today()) == {"AAPL", "NEWC"}
    (tmp_path / "catalysts_today.json").write_text(json.dumps({"date": "2026-01-02",
                                                               "names": [{"symbol": "OLD", "gap_pct": 9}]}))
    assert set(universe.watch_only_today()) == {"AAPL"}


def test_the_gate_sits_before_the_gatekeeper_and_the_order():
    src = open("streamlit_app.py", encoding="utf-8").read()
    gate = src.index('"watch_only_catalyst"')
    assert gate < src.index("analyst.get_verdict(")
    assert gate < src.index("submit_bracket(", gate - 10_000 if gate > 10_000 else 0) \
        or "submit_bracket(" not in src[:gate]
