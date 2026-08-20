"""
Approval auditability (2026-08-20). No network.

Investigation finding: BMNR's approvals were NOT missing reasoning — the
journal held full explanations. floor.py's Note column rendered
`error or rejection_reason`, and an APPROVAL has no rejection_reason, so
every approval read as unexplained. Same class of bug made F's Rule #5
`chop_reclaim` tag row (written approved=1 so it stays out of the
rejection log) look like a phantom gatekeeper approval.
"""

import json
import sqlite3

import pytest

import claude_integration
import floor


# ------------------------------------- harness guard: approvals need reasons

def _gk(monkeypatch, verdict):
    monkeypatch.setattr(claude_integration, "call_claude_api",
                        lambda *a, **k: dict(verdict))
    import pandas as pd
    rows = 20
    df20 = pd.DataFrame({
        "open": [100.0] * rows, "high": [101.0] * rows, "low": [99.0] * rows,
        "close": [100.5] * rows, "volume": [100_000] * rows,
        "ema_fast": [100.4] * rows, "ema_slow": [100.1] * rows,
        "rsi_14": [58.0] * rows, "adx_14": [31.0] * rows})
    return claude_integration.get_gatekeeper_decision(
        ticker="BMNR", df20=df20, ema_spread_pct=0.3, volume_trend="increasing",
        crossover_count=0, dist_to_resistance_pct=0.2, entry_price=21.52,
        stop_price=19.0, target_price=29.0, rr_ratio=3.0, interval_mins=5,
        fast_ema=9, slow_ema=21, news_headlines=[])


def test_approval_without_reasoning_is_refused(monkeypatch, caplog):
    out = _gk(monkeypatch, {"approved": True, "conviction_score": 78,
                            "reasoning": "", "rejection_reason": None})
    assert out["approved"] is False                      # fails SAFE
    assert out["rejection_reason"] == "approval_without_reasoning"
    assert "unauditable" in out["reasoning"]


def test_approval_with_whitespace_only_reasoning_is_refused(monkeypatch):
    out = _gk(monkeypatch, {"approved": True, "conviction_score": 78,
                            "reasoning": "   \n  "})
    assert out["approved"] is False


def test_raw_verdict_is_logged_when_reasoning_missing(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.ERROR, logger="claude_integration")
    _gk(monkeypatch, {"approved": True, "conviction_score": 78, "reasoning": ""})
    blob = " ".join(r.getMessage() for r in caplog.records)
    assert "APPROVED WITHOUT REASONING" in blob
    assert "78" in blob                                   # the raw verdict


def test_explained_approval_survives(monkeypatch):
    out = _gk(monkeypatch, {"approved": True, "conviction_score": 78,
                            "reasoning": "ADX 45.4 with 2.4x volume expansion."})
    assert out["approved"] is True
    assert out["conviction_score"] == 78


def test_rejections_are_untouched(monkeypatch):
    out = _gk(monkeypatch, {"approved": False, "conviction_score": 20,
                            "reasoning": "", "rejection_reason": "adx_low"})
    assert out["approved"] is False
    assert out["rejection_reason"] == "adx_low"           # not overwritten


# ------------------------------------- floor rendering: the actual BMNR bug

def _seed(db, rows):
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, ticker TEXT,
        setup_name TEXT, context TEXT, verdict TEXT, approved INTEGER,
        conviction_score INTEGER, source TEXT, agreement INTEGER)""")
    for r in rows:
        conn.execute("INSERT INTO decisions (timestamp,ticker,setup_name,context,"
                     "verdict,approved,conviction_score,source,agreement) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", r)
    conn.commit(); conn.close()


def test_approval_note_shows_reasoning_not_blank(tmp_path, monkeypatch):
    db = str(tmp_path / "journal.db")
    today = floor._today_et()
    _seed(db, [(f"{today} 11:04:49", "BMNR", "mean_reversion_reclaim", "{}",
                json.dumps({"approved": True, "conviction_score": 78,
                            "rejection_reason": None,
                            "reasoning": "ADX of 45.4 confirms a strong trend, "
                                         "volume expanding 2.4x."}),
                1, 78, "claude", None)])
    monkeypatch.setattr(floor, "JOURNAL_DB", db)
    conn = floor._conn()
    out = "\n".join(floor.gatekeeper_section(conn, today, {}))
    conn.close()
    assert "✅ approved" in out
    assert "ADX of 45.4 confirms a strong trend" in out     # was blank before


def test_tag_row_is_not_rendered_as_an_approval(tmp_path, monkeypatch):
    """F 2026-08-17: a chop_reclaim tag carries approved=1 by design."""
    db = str(tmp_path / "journal.db")
    today = floor._today_et()
    _seed(db, [(f"{today} 15:01:18", "F", "mean_reversion_reclaim", "{}",
                json.dumps({"approved": True, "tag": "chop_reclaim",
                            "rejection_reason": None, "source": "rules"}),
                1, None, "ceo", None)])
    monkeypatch.setattr(floor, "JOURNAL_DB", db)
    conn = floor._conn()
    out = "\n".join(floor.gatekeeper_section(conn, today, {}))
    conn.close()
    assert "tag:chop_reclaim" in out
    assert "✅ approved" not in out                          # no phantom approval


def test_rejection_note_still_shows_the_reason(tmp_path, monkeypatch):
    db = str(tmp_path / "journal.db")
    today = floor._today_et()
    _seed(db, [(f"{today} 11:04:52", "BMNR", "mean_reversion_reclaim", "{}",
                json.dumps({"approved": False, "conviction_score": 35,
                            "rejection_reason": "RSI overextension",
                            "reasoning": "long explanation here"}),
                0, 35, "local_shadow", 0)])
    monkeypatch.setattr(floor, "JOURNAL_DB", db)
    conn = floor._conn()
    out = "\n".join(floor.gatekeeper_section(conn, today, {}))
    conn.close()
    assert "❌ rejected" in out
    assert "RSI overextension" in out       # reason wins over reasoning
