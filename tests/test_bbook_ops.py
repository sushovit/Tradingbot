"""
Goals 19-21 — B-book, SET_STOP, short-lane isolation, ops. No network.
"""

import os
import sqlite3
from datetime import datetime

import pandas as pd
import pytest
import pytz

import backtest
import journal as journal_mod
import risk
from orders import validate_order

EASTERN_TZ = pytz.timezone("US/Eastern")
NOW = EASTERN_TZ.localize(datetime(2026, 7, 27, 10, 0))


def order(**over):
    o = {"action": "BUY", "ticker": "NVDA", "notional_usd": 200,
         "entry": 100.0, "stop": 95.0, "target": 110.0,
         "setup": "trend_continuation"}
    o.update(over)
    return o


# =============================================================== Goal 19

def test_tier_risk_and_gates():
    assert risk.tier_risk_pct("B", 1.0) == 0.5
    assert risk.tier_risk_pct("A", 1.0) == 1.0
    assert risk.check_tier_b(0, 0) == (True, None)
    assert risk.check_tier_b(1, 0)[1] == "b_book_position_open"
    assert risk.check_tier_b(0, 1)[1] == "b_book_weekly_limit"


def test_invalid_tier_rejected():
    ok, reason = validate_order(order(tier="C"), 2000.0, 0, 3, NOW)
    assert not ok and reason.startswith("invalid_tier")


def test_second_b_entry_in_week_rejected(temp_journal, monkeypatch):
    monkeypatch.setattr("orders.journal", temp_journal)
    # First B entry this week, still open.
    temp_journal.log_trade("AMD", "BUY", 5, 50.0, reason="CEO test", tier="B")
    ok, reason = validate_order(order(tier="B", notional_usd=20), 2000.0, 0, 3, NOW)
    assert not ok
    assert reason in ("b_book_position_open", "b_book_weekly_limit")


def test_b_risk_cap_enforced(temp_journal, monkeypatch):
    monkeypatch.setattr("orders.journal", temp_journal)
    # $2,000 equity, 0.5% = $10 risk. entry 100 / stop 95 -> 5% risk per share
    # -> max notional about $200. A $400 B order must be rejected.
    ok, reason = validate_order(order(tier="B", notional_usd=400),
                                2000.0, 0, 3, NOW)
    assert not ok and reason == "b_book_risk_exceeded"
    ok, reason = validate_order(order(tier="B", notional_usd=190),
                                2000.0, 0, 3, NOW)
    assert ok, reason


def test_a_stats_exclude_b_rows(temp_journal):
    temp_journal.log_trade("NVDA", "SELL", 2, 110.0, pnl_usd=20.0, tier="A")
    temp_journal.log_trade("AMD", "SELL", 1, 40.0, pnl_usd=-7.0, tier="B")
    assert temp_journal.tier_realized_pnl("A") == pytest.approx(20.0)
    assert temp_journal.tier_realized_pnl("B") == pytest.approx(-7.0)


def test_open_b_tickers_tracks_lifecycle(temp_journal):
    temp_journal.log_trade("AMD", "BUY", 5, 50.0, tier="B")
    assert temp_journal.open_b_tickers() == ["AMD"]
    temp_journal.log_trade("AMD", "SELL", 5, 52.0, pnl_usd=10.0, tier="B")
    assert temp_journal.open_b_tickers() == []


def test_legacy_trades_default_to_tier_a(temp_journal):
    temp_journal.log_trade("NVDA", "SELL", 1, 10.0, pnl_usd=5.0)   # no tier arg
    assert temp_journal.tier_realized_pnl("A") == pytest.approx(5.0)


# =============================================================== Goal 21

def test_set_stop_widen_requires_explicit_flag_and_reason():
    base = {"action": "SET_STOP", "ticker": "NVDA", "stop": 90.0,
            "current_stop": 95.0}
    ok, reason = validate_order(dict(base), 2000.0, 0, 3, NOW)
    assert not ok and reason == "widen_requires_allow_widen"
    ok, reason = validate_order(dict(base, allow_widen=True), 2000.0, 0, 3, NOW)
    assert not ok and reason == "widen_requires_reason"
    ok, _ = validate_order(dict(base, allow_widen=True,
                                reason="thesis intact, wider base"),
                           2000.0, 0, 3, NOW)
    assert ok


def test_tighten_stop_alias_can_only_tighten():
    ok, reason = validate_order({"action": "TIGHTEN_STOP", "ticker": "NVDA",
                                 "stop": 90.0, "current_stop": 95.0,
                                 "allow_widen": True, "reason": "nope"},
                                2000.0, 0, 3, NOW)
    assert not ok and reason == "tighten_stop_cannot_widen"
    ok, _ = validate_order({"action": "TIGHTEN_STOP", "ticker": "NVDA",
                            "stop": 97.0, "current_stop": 95.0},
                           2000.0, 0, 3, NOW)
    assert ok


def test_nvda_profile_is_moderate():
    import json
    with open("bot_config.json") as f:
        cfg = json.load(f)
    assert cfg["ticker_profiles"]["NVDA"] == "Moderate"


def test_key_outage_error_purge_migration(temp_journal):
    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.execute(
        "INSERT INTO decisions (timestamp, ticker, setup_name, context, verdict,"
        " approved, source) VALUES ('2026-07-22 10:15:33','CIFR','x','{}',"
        " '{\"error\": \"ANTHROPIC_API_KEY not found in environment variables.\"}',"
        " 0, 'claude')")
    conn.execute(
        "INSERT INTO decisions (timestamp, ticker, setup_name, context, verdict,"
        " approved, source) VALUES ('2026-07-22 11:00:00','NVDA','x','{}',"
        " '{\"approved\": true}', 1, 'claude')")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.execute("DELETE FROM meta WHERE key LIKE 'mig_%'")
    conn.commit()
    conn.close()
    temp_journal.run_data_migrations()

    conn = sqlite3.connect(temp_journal.DB_FILE)
    remaining = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE timestamp LIKE '2026-07-22%'"
    ).fetchone()[0]
    conn.close()
    assert remaining == 1          # real verdict kept, ERROR row purged


# =============================================================== Goal 20

def test_short_detectors_are_research_only():
    """The short lane must exist ONLY in backtest.py — no live path may
    import it, and orders.py still has no SELL-to-open action."""
    import ast
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for fname in ("orders.py", "streamlit_app.py", "intern_trader.py"):
        with open(os.path.join(root, fname), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            assert "backtest" not in names, f"{fname} imports the short lane"
    from orders import VALID_ACTIONS
    assert "SHORT" not in VALID_ACTIONS and "SELL_SHORT" not in VALID_ACTIONS


def _short_df(rows):
    idx = pd.bdate_range("2026-01-05", periods=len(rows))
    return pd.DataFrame(rows, index=idx,
                        columns=["open", "high", "low", "close", "volume"])


def test_breakdown_detector_and_short_fills():
    rows = [(100, 101, 99, 100, 100_000)] * 25
    rows.append((100, 100.5, 94.0, 94.5, 200_000))     # breakdown bar
    rows.append((94, 95, 93, 93.5, 150_000))
    df = _short_df(rows)
    res = backtest.detect_breakdown_continuation(df.iloc[-25:])
    assert isinstance(res, dict)
    assert res["stop_level"] == pytest.approx(100.5)   # stop ABOVE the bar high

    # Short bracket: entry 94, stop 100 -> target at 2R = 82.
    d = _short_df([(94, 95, 93, 94, 1e6), (90, 91, 81, 82, 1e6)])
    price, i, reason = backtest.simulate_bracket_short(d, 0, 94.0, 100.0, 82.0)
    assert (price, reason) == (82.0, "target")
    d2 = _short_df([(94, 95, 93, 94, 1e6), (94, 101, 93, 100, 1e6)])
    price, i, reason = backtest.simulate_bracket_short(d2, 0, 94.0, 100.0, 82.0)
    assert (price, reason) == (100.0, "stop")
