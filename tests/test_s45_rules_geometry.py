"""CEO 2026-10-22 (A): every rules rejection is journaled with the entry,
stop and target the signal would have had, so the outcome tracker grades it;
the goals report shows per-blocker n / win % / avg R weekly. Evidence only."""

import json
import sqlite3
from datetime import date

import numpy as np
import pandas as pd

import outcomes
from strategies.base import Rejection, would_be


def test_would_be_rejects_invalid_long_geometry():
    assert would_be(100, 95, 115) == {"entry": 100.0, "stop": 95.0, "target": 115.0}
    assert would_be(100, 100) is None and would_be(100, 101) is None
    assert would_be(None, 1) is None


def test_journal_stores_geometry_the_tracker_can_read(temp_journal):
    rid = temp_journal.log_rules_pass("AMD", "momentum_continuation", "volume_low",
                                      "Breakout volume low", bar_key="2026-10-21",
                                      geometry={"entry": 100.0, "stop": 95.0, "target": 115.0})
    ctx = sqlite3.connect(temp_journal.DB_FILE).execute(
        "SELECT context FROM decisions WHERE id=?", (rid,)).fetchone()[0]
    assert json.loads(ctx)["entry"] == 100.0
    assert outcomes.geometry(ctx) == (100.0, 95.0, 115.0)


def test_rows_without_geometry_are_unchanged(temp_journal):
    rid = temp_journal.log_rules_pass("AMD", "x", "outside_hours", "")
    ctx = json.loads(sqlite3.connect(temp_journal.DB_FILE).execute(
        "SELECT context FROM decisions WHERE id=?", (rid,)).fetchone()[0])
    assert ctx == {"details": ""}


def momentum_bars(breakout_volume):
    n = 25
    close = np.r_[np.full(22, 100.0), [100.5, 104.0, 104.5]]
    high = close + 0.5
    low = close - 0.5
    vol = np.r_[np.full(23, 1000.0), [breakout_volume, 1000.0]]
    idx = pd.date_range("2026-09-01", periods=n, freq="B")
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close,
                         "volume": vol}, index=idx)


def test_momentum_volume_rejection_carries_its_would_be_trade():
    from strategies.momentum_continuation import MomentumContinuation
    out = MomentumContinuation().detect(momentum_bars(1100.0),
                                        {"ticker": "X", "config": {}, "risk_profile": {}})
    assert isinstance(out, Rejection) and out.filter_name == "volume_low"
    g = out.geometry
    assert g["entry"] == 104.5 and g["stop"] == 103.5          # breakout bar low
    assert g["target"] == 104.5 + 1.0 * 3.0


def test_reclaim_below_ema9_rejection_carries_its_would_be_trade():
    from strategies.mean_reversion_reclaim import MeanReversionReclaim
    # 20 bars sliding 120 -> 100 (washout), then a reclaim above the prior
    # high that still closes below EMA9
    close = np.r_[np.linspace(120, 100, 21), [100.3, 100.8]]
    idx = pd.date_range("2026-09-01", periods=len(close), freq="B")
    df = pd.DataFrame({"open": close, "high": close + 0.2, "low": close - 0.4,
                       "close": close, "volume": 1000.0}, index=idx)
    df.loc[df.index[-3], "high"] = 100.1           # prior bar high, reclaimed by 100.3
    out = MeanReversionReclaim().detect(df, {"ticker": "X", "config": {}, "risk_profile": {}})
    assert isinstance(out, Rejection) and out.filter_name == "below_ema9"
    assert out.geometry["entry"] == 100.8 and out.geometry["stop"] == round(100.3 - 0.4, 4)


def test_trend_adx_rejection_carries_its_would_be_trade(monkeypatch):
    import strategies.trend_continuation as tc
    from tests.test_s43_sizing_adx import golden_cross_df
    monkeypatch.setattr(tc.ta, "adx", lambda h, l, c, length=14: pd.DataFrame(
        {"ADX_14": [20.0] * len(c)}, index=c.index))
    ctx = {"ticker": "X", "config": {"use_rsi_filter": False, "use_confirmation_candle": False},
           "risk_profile": {"fast_ema": 9, "slow_ema": 21, "adx_threshold": 30,
                            "rr_ratio": 3.0, "atr_multiplier": 2.0, "use_volume_filter": False}}
    out = tc.TrendContinuation().detect(golden_cross_df(), ctx)
    assert out.filter_name == "adx_low"
    g = out.geometry
    assert g["stop"] < g["entry"] < g["target"]
    assert abs((g["target"] - g["entry"]) - 3 * (g["entry"] - g["stop"])) < 1e-3   # 4-dp rounding


def test_worker_journals_geometry_for_detector_and_gate_rejections(temp_journal, tmp_path,
                                                                   monkeypatch):
    """End to end through the worker loop: a detector rejection (carrying
    geometry) and a gate after the signal (watch-only) both store entry/stop."""
    import streamlit_app as app
    import tests.test_s36_watch_only as w

    class Rejecting:
        name, timeframe = "fake_intraday_setup", "intraday"

        def detect(self, df, context):
            e = float(df["close"].iloc[-1])
            return Rejection(self.name, context["ticker"], "volume_low", "low",
                             geometry=would_be(e, e * 0.97, e * 1.09))

    def strategies_for(t, c):
        return [Rejecting()] if t == "AMD" else [w.FakeDaily()]
    monkeypatch.setattr(app, "enabled_strategies", strategies_for)
    import analyst
    import daily_eval
    import universe
    monkeypatch.setattr(daily_eval, "gap_abort", lambda df, px: (False, 0, 0))
    monkeypatch.setattr(universe, "watch_only_today", lambda: {"NVDA": {"gap_pct": 5, "tag": "x"}})
    monkeypatch.setattr(analyst, "get_verdict", lambda *a, **k: (
        {"approved": False, "conviction_score": 10, "rejection_reason": "t"}, None))
    import tests.test_s19b_async_discord as harness

    def bars(self, tickers, timeframe_minutes=5, lookback_days=5):
        idx = pd.date_range("2026-10-14 13:30", periods=40, freq="5min", tz="UTC")
        px = [759.0 + 0.01 * i for i in range(40)]
        return {t: pd.DataFrame({"open": px, "high": px, "low": px, "close": px,
                                 "volume": 1000}, index=idx) for t in tickers}
    monkeypatch.setattr(harness.WorkerBroker, "get_bars", bars)
    monkeypatch.setattr(harness.WorkerBroker, "get_latest_price", lambda self, t: 759.4,
                        raising=False)
    harness._run_worker(app, monkeypatch, tmp_path, cycles=1)
    conn = sqlite3.connect(temp_journal.DB_FILE)
    rows = {(t, json.loads(v)["rejection_reason"]): json.loads(c) for t, v, c in conn.execute(
        "SELECT ticker, verdict, context FROM decisions WHERE source='rules'")}
    assert rows[("AMD", "volume_low")]["entry"] > rows[("AMD", "volume_low")]["stop"]
    assert rows[("NVDA", "watch_only_catalyst")]["entry"] > rows[("NVDA", "watch_only_catalyst")]["stop"]


def test_goals_report_has_a_weekly_per_blocker_table(temp_journal):
    import goals
    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.execute("""CREATE TABLE IF NOT EXISTS rejection_outcomes (
        decision_id INTEGER PRIMARY KEY, decided_date TEXT NOT NULL, ticker TEXT NOT NULL,
        setup_name TEXT, source TEXT NOT NULL, reason_key TEXT NOT NULL, reason_raw TEXT,
        entry REAL, stop REAL, target REAL, outcome TEXT NOT NULL, r_achieved REAL,
        sessions_used INTEGER, resolved INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)""")
    rows = [(1, "volume_low", "target", 3.0, 1), (2, "volume_low", "stop", -1.0, 1),
            (3, "volume_low", "neither", None, 0), (4, "adx_low", "no_geometry", None, 1)]
    for i, k, o, r, res in rows:
        conn.execute("INSERT INTO rejection_outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (i, "2026-10-22", "X", "s", "rules", k, "", 100, 95, 115, o, r, 1, res, "now"))
    stats = {s["blocker"]: s for s in goals.blocker_stats(conn, "2026-10-19", "2026-10-23")}
    assert stats["volume_low"]["rejected"] == 3 and stats["volume_low"]["graded"] == 2
    assert stats["volume_low"]["win_pct"] == 50.0 and stats["volume_low"]["avg_r"] == 1.0
    assert stats["adx_low"]["graded"] == 0
    text = "\n".join(goals.blocker_lines(conn, "2026-10-19", "2026-10-23"))
    assert "| volume_low | 3 | 2 | 50% | +1.00 |" in text
