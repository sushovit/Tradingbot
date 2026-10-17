"""
CEO 2026-10-17 (A): gatekeeper approvals below the conviction threshold are
blocks - the worker never trades them - so the rejection-outcome tracker
grades them too, under reason gatekeeper_low_conviction. Study only.
"""

import json

import pandas as pd

import outcomes


def decision(journal, ticker, approved, conviction, source="claude",
             setup="mean_reversion_reclaim"):
    return journal.log_decision(
        ticker, setup, {"entry": 100.0, "stop": 95.0, "target": 115.0},
        {"approved": approved, "conviction_score": conviction,
         "rejection_reason": None if approved else "RSI falling"},
        source=source)


def test_low_conviction_approvals_are_picked_up_and_traded_ones_are_not(temp_journal):
    low = decision(temp_journal, "META", True, 58)
    traded = decision(temp_journal, "TXN", True, 72)
    rejected = decision(temp_journal, "TMO", False, 20)
    ids = {r["id"] for r in outcomes.pending_decisions(
        include_unresolved=False, date_str=_today(temp_journal), threshold=70)}
    assert low in ids and rejected in ids
    assert traded not in ids


def _today(journal):
    import sqlite3
    c = sqlite3.connect(journal.DB_FILE)
    d = c.execute("SELECT substr(MAX(timestamp),1,10) FROM decisions").fetchone()[0]
    c.close()
    return d


def bars_after(day, rows):
    idx = pd.bdate_range(start=pd.Timestamp(day) + pd.Timedelta(days=1),
                         periods=len(rows))
    o, h, l, c = zip(*rows)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c}, index=idx)


def test_evaluate_grades_them_under_their_own_key(temp_journal):
    low = decision(temp_journal, "META", True, 58)
    day = _today(temp_journal)
    bars = {"META": bars_after(day, [(101, 116, 100, 115)])}       # hits target
    outcomes.evaluate(day, bars_by_ticker=bars, db_file=temp_journal.DB_FILE)
    import sqlite3
    c = sqlite3.connect(temp_journal.DB_FILE)
    row = c.execute("SELECT reason_key, reason_raw, outcome, r_achieved "
                    "FROM rejection_outcomes WHERE decision_id=?", (low,)).fetchone()
    c.close()
    assert row[0] == "gatekeeper_low_conviction"
    assert "conviction 58" in row[1]
    assert row[2] == "target" and row[3] == 3.0


def test_class_summary(temp_journal):
    day = None
    for t, rows in (("A", [(101, 116, 100, 115)]),            # +3R
                    ("B", [(100, 100, 94, 95)]),               # -1R
                    ("C", [(101, 116, 100, 115)])):            # +3R
        decision(temp_journal, t, True, 60)
        day = _today(temp_journal)
    bars = {t: bars_after(day, r) for t, r in (("A", [(101, 116, 100, 115)]),
                                               ("B", [(100, 100, 94, 95)]),
                                               ("C", [(101, 116, 100, 115)]))}
    outcomes.evaluate(day, bars_by_ticker=bars, db_file=temp_journal.DB_FILE)
    s = outcomes.class_summary("gatekeeper_low_conviction", day,
                               temp_journal.DB_FILE)
    assert s["n"] == 3 and s["graded"] == 3
    assert s["win_pct"] == 66.7
    assert s["avg_r"] == round((3 - 1 + 3) / 3, 3)


def test_threshold_comes_from_config():
    cfg = json.load(open("bot_config.json", encoding="utf-8"))
    assert outcomes.conviction_threshold(cfg) == float(
        cfg.get("claude_conviction_threshold", 70))
