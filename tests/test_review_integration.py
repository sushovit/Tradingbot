"""
Review-bot integration (2026-08-13). No network.

  1. bundle carries governance rows + live bracket geometry (R:R verifiable)
  2. risk-free positions are flagged, killing the BAC REVIEW-DUE false positive
  3. drop.py appends the latest review memo as its final section
  4. duplicate rules-passes are impossible at the DB, and counters self-explain
"""

import json
import os

import pytest

import drop
import journal as journal_mod
import review_bot


# ------------------------------------------------------------------ 4

def test_rules_pass_is_idempotent_across_processes(temp_journal):
    """Two workers (or a restart) journalling the same pass must produce ONE
    row — the in-memory cache cannot span processes."""
    a = temp_journal.log_rules_pass("XOM", "trend_continuation", "adx_low", "x")
    b = temp_journal.log_rules_pass("XOM", "trend_continuation", "adx_low", "x")
    c = temp_journal.log_rules_pass("XOM", "trend_continuation", "adx_low", "y")
    assert a == b == c                       # same row returned every time
    rows = temp_journal.intern_calls  # keep import used
    counts = temp_journal.decision_counts()
    assert counts["rules_passes"] == 1


def test_different_filters_and_tickers_still_journal(temp_journal):
    temp_journal.log_rules_pass("XOM", "trend_continuation", "adx_low")
    temp_journal.log_rules_pass("XOM", "trend_continuation", "volume_low")
    temp_journal.log_rules_pass("NOK", "trend_continuation", "adx_low")
    assert temp_journal.decision_counts()["rules_passes"] == 3


def test_decision_counts_are_self_explaining(temp_journal):
    temp_journal.log_rules_pass("XOM", "trend_continuation", "adx_low")
    temp_journal.log_decision("NVDA", "momentum_continuation", {},
                              {"approved": True, "conviction_score": 80},
                              source="claude")
    temp_journal.log_decision("NVDA", "momentum_continuation", {},
                              {"approved": False, "conviction_score": 40},
                              source="local_shadow")
    c = temp_journal.decision_counts()
    assert c["total"] == 3
    assert c["gatekeeper_calls"] == 2        # claude + shadow
    assert c["rules_passes"] == 1
    assert c["by_source"]["claude"] == 1


# ------------------------------------------------------------------ 1

def test_governance_rows_capture_ceo_and_tags(temp_journal):
    temp_journal.log_decision("SPCX", "event_flow", {"order": {}},
                              {"approved": True, "rejection_reason": None,
                               "reasoning": "index inclusion flow"},
                              source="ceo")
    temp_journal.log_signal_tag("IREN", "mean_reversion_reclaim",
                                "chop_reclaim", "Rule #5 exemption")
    temp_journal.log_decision("NVDA", "trend_continuation", {},
                              {"approved": False, "conviction_score": 30},
                              source="claude")      # NOT governance
    rows = temp_journal.governance_rows()
    tickers = {r["ticker"] for r in rows}
    assert tickers == {"SPCX", "IREN"}
    tagged = [r for r in rows if r["tag"] == "chop_reclaim"]
    assert len(tagged) == 1


# ------------------------------------------------------------------ 1 + 2

class _Pos:
    def __init__(self, symbol, qty, entry, current):
        self.symbol, self.qty = symbol, qty
        self.avg_entry_price, self.current_price = entry, current
        self.unrealized_pl = (current - entry) * qty


class _Ord:
    def __init__(self, symbol, order_type, stop=None, limit=None):
        self.symbol, self.order_type = symbol, order_type
        self.stop_price, self.limit_price = stop, limit


def test_bundle_has_geometry_and_riskfree_flag(tmp_path, monkeypatch,
                                               temp_journal):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(review_bot, "journal", temp_journal)

    class FakeBroker:
        def __init__(self, *a, **k):
            pass

        def get_positions(self):
            return [_Pos("BAC", 7, 61.78, 63.00),      # stop ABOVE entry
                    _Pos("NOK", 4, 10.76, 10.90)]      # stop below entry

        def get_live_orders(self, ticker=None):
            return [_Ord("BAC", "stop", stop=62.50),
                    _Ord("BAC", "limit", limit=66.00),
                    _Ord("NOK", "stop", stop=10.21),
                    _Ord("NOK", "limit", limit=12.31)]

        def get_equity(self):
            return 2000.0

    monkeypatch.setitem(__import__("sys").modules, "broker",
                        type("m", (), {"Broker": FakeBroker}))
    monkeypatch.setattr(review_bot, "position_sessions_held", lambda t: 12)

    bundle = review_bot.collect_bundle()
    bac = next(p for p in bundle["positions"] if p["ticker"] == "BAC")
    nok = next(p for p in bundle["positions"] if p["ticker"] == "NOK")

    assert bac["risk_free"] is True            # 62.50 >= 61.78 entry
    assert nok["risk_free"] is False
    assert bac["stop"] == 62.50 and bac["target"] == 66.00
    assert nok["rr_remaining"] is not None     # R:R now verifiable
    assert nok["stop_pct"] is not None and nok["target_pct"] is not None

    prompt = review_bot.build_user_prompt(bundle)
    assert "RISK-FREE" in prompt
    # A risk-free position held 12 sessions must NOT be nagged.
    bac_line = [ln for ln in prompt.splitlines() if ln.startswith("- BAC")][0]
    assert "REVIEW DUE" not in bac_line
    nok_line = [ln for ln in prompt.splitlines() if ln.startswith("- NOK")][0]
    assert "REVIEW DUE" in nok_line            # 12 sessions, still at risk
    assert "GOVERNANCE DECISIONS TODAY" in prompt
    assert "DIFFERENT counters" in prompt      # the count finding, answered


# ------------------------------------------------------------------ 3

def test_drop_appends_review_memo_last(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "review_2026-08-12.md").write_text("OLD MEMO",
                                                               encoding="utf-8")
    (tmp_path / "reports" / "review_2026-08-13.md").write_text(
        "# Daily review — 2026-08-13\nMark the book: flat.", encoding="utf-8")
    memo = drop.latest_review_memo()
    assert "2026-08-13" in memo                # newest, not the old one
    assert "OLD MEMO" not in memo

    md = drop.assemble([("report", "body"),
                        ("CEO REVIEW MEMO (latest)", memo)])
    assert md.rstrip().endswith("Mark the book: flat.")   # final section


def test_drop_memo_absent_is_graceful(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert "no review memo yet" in drop.latest_review_memo()
