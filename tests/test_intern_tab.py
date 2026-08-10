"""
Goal 12c — Intern Desk tab data providers. No network, no Streamlit.

  - status file write/read roundtrip; active while fresh & unfinished
  - stale status -> inactive; missing/corrupt -> None (never crashes)
  - report card + history providers safe on empty data
"""

import json
import os
import time

import intern_desk


def test_status_roundtrip_active(tmp_path, monkeypatch):
    monkeypatch.setattr(intern_desk, "STATUS_FILE", str(tmp_path / "s.json"))
    intern_desk._write_status({"started_at": "2026-07-18T15:35:00",
                               "finished_at": None, "model": "qwen3:4b",
                               "current_ticker": "MRVL", "done_count": 23,
                               "total": 40, "last_verdicts": [["NVDA", "no_trade", 85]]})
    s = intern_desk.read_status()
    assert s is not None
    assert s["active"] is True                      # fresh + unfinished
    assert s["current_ticker"] == "MRVL"
    assert s["done_count"] == 23 and s["total"] == 40
    assert s["last_verdicts"] == [["NVDA", "no_trade", 85]]
    assert s["age_secs"] < 60


def test_finished_run_not_active_even_if_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(intern_desk, "STATUS_FILE", str(tmp_path / "s.json"))
    intern_desk._write_status({"started_at": "x", "finished_at": "y",
                               "model": "m", "current_ticker": None,
                               "done_count": 40, "total": 40,
                               "last_verdicts": []})
    s = intern_desk.read_status()
    assert s["active"] is False


def test_stale_status_inactive(tmp_path, monkeypatch):
    path = tmp_path / "s.json"
    monkeypatch.setattr(intern_desk, "STATUS_FILE", str(path))
    intern_desk._write_status({"started_at": "x", "finished_at": None,
                               "model": "m", "current_ticker": "AMD",
                               "done_count": 5, "total": 40,
                               "last_verdicts": []})
    old = time.time() - 300
    os.utime(path, (old, old))                      # file went quiet 5 min ago
    s = intern_desk.read_status()
    assert s is not None
    assert s["active"] is False
    assert s["age_secs"] >= 300


def test_missing_and_corrupt_status(tmp_path, monkeypatch):
    monkeypatch.setattr(intern_desk, "STATUS_FILE", str(tmp_path / "nope.json"))
    assert intern_desk.read_status() is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setattr(intern_desk, "STATUS_FILE", str(bad))
    assert intern_desk.read_status() is None
    lst = tmp_path / "list.json"
    lst.write_text(json.dumps([1, 2, 3]))
    monkeypatch.setattr(intern_desk, "STATUS_FILE", str(lst))
    assert intern_desk.read_status() is None


def test_report_card_empty():
    card = intern_desk.build_report_card([])
    assert card["total_calls"] == 0
    assert card["graded"] == 0
    assert card["grade_rate_pct"] is None
    assert card["good_pct"] is None
    assert card["by_stance"] == {}


def test_report_card_aggregates():
    rows = [
        {"stance": "long_setup", "grade": "good"},
        {"stance": "long_setup", "grade": "bad"},
        {"stance": "no_trade", "grade": "good"},
        {"stance": "no_trade", "grade": "ungradeable"},
        {"stance": "no_trade", "grade": None},
    ]
    card = intern_desk.build_report_card(rows)
    assert card["total_calls"] == 5
    assert card["graded"] == 4
    assert card["good"] == 2 and card["bad"] == 1 and card["ungradeable"] == 1
    assert card["grade_rate_pct"] == 80.0
    assert card["good_pct"] == pytest_approx(66.7)
    assert card["by_stance"] == {"long_setup": 2, "no_trade": 3}


def pytest_approx(v):
    import pytest
    return pytest.approx(v, abs=0.05)


def test_intern_history_empty(temp_journal):
    assert temp_journal.intern_history() == []


# =============================================================================
# Conviction calibration v2 — prompt version + weekend guard
# =============================================================================

def test_prompt_version_exported_and_current():
    import prompts
    # The desk must export whatever version prompts.py declares — they can
    # never drift apart, which is what this pins.
    assert prompts.INTERN_PROMPT_VERSION >= 3
    assert intern_desk.INTERN_PROMPT_VERSION == prompts.INTERN_PROMPT_VERSION
    # The v2 anchors and rebalanced framing are actually in the prompt.
    p = prompts.build_intern_desk_prompt("NVDA", "candles", "metrics", "news")
    for anchor in ("15 = barely a setup", "70 = good setup, one clear concern",
                   "90+ = exceptional confluence", "never auto-zero",
                   "scores that don't match your stated reasoning"):
        assert anchor in p, f"missing anchor text: {anchor}"


def test_weekend_guard_trade_is_clean_noop(temp_journal, monkeypatch):
    import sqlite3
    from datetime import datetime
    import intern_trader

    # Saturday 2026-07-18 in ET.
    monkeypatch.setattr(intern_trader, "is_trading_day", lambda dt=None: False)

    class NeverBroker:
        def __getattr__(self, name):
            raise AssertionError("broker must not be touched on a weekend")

    line = intern_trader.execute_trade(
        {"NVDA": {"stance": "long_setup", "setup_name": "momentum_continuation",
                  "conviction": 95, "invalidation": 90.0, "key_risk": "r",
                  "reasoning": "great"}},
        broker=NeverBroker())
    assert "market closed" in line

    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM decisions WHERE source='intern'").fetchall()
    conn.close()
    assert len(rows) == 1
    assert "market_closed" in rows[0]["verdict"]


def test_weekday_is_trading_day():
    import intern_trader
    from datetime import datetime
    assert intern_trader.is_trading_day(datetime(2026, 7, 17)) is True   # Fri
    assert intern_trader.is_trading_day(datetime(2026, 7, 18)) is False  # Sat
    assert intern_trader.is_trading_day(datetime(2026, 7, 19)) is False  # Sun
