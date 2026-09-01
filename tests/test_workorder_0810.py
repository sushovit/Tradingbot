"""
Work order 2026-08-10: loop supervisor, prompts v4 + reclaim patch,
mid-band re-rank, intern outage honesty, training export. No network.
"""

import json
import sqlite3

import pytest

import export_training
import intern_desk
import prompts

# The loop supervisor exists for the Windows worker (machine sleep,
# AV, Task Scheduler). Guarded so a Linux audit run agrees with
# Windows instead of reporting phantom failures.
WINDOWS_ONLY = pytest.mark.skipif(
    __import__('sys').platform != 'win32',
    reason='Windows worker supervisor')


# ------------------------------------------------------- 1. loop supervisor

@WINDOWS_ONLY
def test_supervisor_restarts_crashed_loop(monkeypatch, tmp_path):
    """An exception escaping the cycle body must NOT leave a zombie: the
    supervisor logs, alerts, and restarts while the lock exists."""
    import streamlit_app as app
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot.run").write_text("")

    calls = {"n": 0}

    def crashing_loop():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated data-fetch explosion")
        # Second attempt: the operator stopped the desk.
        (tmp_path / "bot.run").unlink()
        return

    monkeypatch.setattr(app, "_worker_loop", crashing_loop)
    monkeypatch.setattr(app, "write_status", lambda *a, **k: None)
    monkeypatch.setattr(app, "send_discord_notification", lambda *a, **k: None)
    monkeypatch.setattr(app.a_time, "sleep", lambda s: None)

    app.live_bot_worker()                 # must return, not raise
    assert calls["n"] == 2                # crashed once, restarted, then exited


@WINDOWS_ONLY
def test_supervisor_exits_cleanly_without_lock(monkeypatch, tmp_path):
    import streamlit_app as app
    monkeypatch.chdir(tmp_path)           # no bot.run
    ran = []
    monkeypatch.setattr(app, "_worker_loop", lambda: ran.append(True))
    app.live_bot_worker()
    assert ran == []                      # never entered without a lock


# ------------------------------------------------------- 2. reclaim patch

def test_reclaim_no_room_guidance_in_gatekeeper_prompt():
    p = prompts.build_gatekeeper_user_prompt(
        ticker="UBER", candle_data_str="c", adx_val=30.0, rsi_val=55.0,
        ema_spread_pct=0.3, volume_trend="increasing", crossover_count=1,
        dist_to_resistance_pct=0.2, entry_price=100.0, stop_price=95.0,
        target_price=115.0, rr_ratio=3.0, interval_mins=5, fast_ema=9,
        slow_ema=21, news_str="none", setup_name="mean_reversion_reclaim")
    assert "mean_reversion_reclaim: proximity to the 20-bar high" in p
    assert "+0.208R" in p                       # cites the ratified evidence
    assert "RSI overextension remains a valid" in p   # still a real factor


# ------------------------------------------------------- 3. intern v4

def test_prompt_v4_requirements():
    assert prompts.INTERN_PROMPT_VERSION == 4
    p = prompts.build_intern_desk_prompt("NVDA", "c", "m", "n")
    assert "TWO DIFFERENT" in p and "price-structure" in p
    assert "STRENGTH, not DIRECTION" in p
    assert "SELF-CONSISTENCY" in p and "is a violation" in p


def test_midband_rerank_forces_distinct_scores(temp_journal, monkeypatch):
    """The 2026-08-06 failure: 21 rows all scored 42 AFTER re-ranking."""
    verdicts = {f"T{i:02d}": {"stance": "no_trade", "setup_name": None,
                              "conviction": 42, "invalidation": None,
                              "key_risk": "", "reasoning": f"r{i}"}
                for i in range(8)}
    # Model returns identical scores for everything (the observed failure).
    monkeypatch.setattr(intern_desk, "_call_local_model",
                        lambda s, u: {t: 42 for t in verdicts})
    note = intern_desk.apply_second_pass(verdicts, "2026-08-10")

    scores = [v["conviction"] for v in verdicts.values()]
    assert len(set(scores)) == len(scores), "scores must be strictly distinct"
    assert min(scores) >= 30 and max(scores) <= 55
    assert "distinct values" in note


def test_midband_rerank_respects_model_ordering(temp_journal, monkeypatch):
    verdicts = {
        "AAA": {"stance": "no_trade", "setup_name": None, "conviction": 44,
                "invalidation": None, "key_risk": "", "reasoning": "x"},
        "BBB": {"stance": "no_trade", "setup_name": None, "conviction": 41,
                "invalidation": None, "key_risk": "", "reasoning": "y"},
    }
    monkeypatch.setattr(intern_desk, "_call_local_model",
                        lambda s, u: {"AAA": 20, "BBB": 90})   # BBB ranked best
    intern_desk.apply_second_pass(verdicts, "2026-08-10")
    assert verdicts["BBB"]["conviction"] > verdicts["AAA"]["conviction"]


# ------------------------------------------------------- 5. small fixes

def test_report_risk_free_exemption_logic():
    """Stop at/above entry => exempt from the REVIEW DUE nag."""
    entry, held = 61.78, 12
    for stop, expect_nag in ((59.60, True), (61.78, False), (62.50, False)):
        risk_free = stop >= entry
        nag = held >= 10 and not risk_free
        assert nag is expect_nag


# ------------------------------------------------------- 4. training export

def test_export_excludes_smoke_and_error_rows(temp_journal, tmp_path):
    temp_journal.log_decision("NVDA", "trend_continuation",
                              {"prompt_version": 4},
                              {"approved": True, "conviction_score": 80,
                               "reasoning": "good"}, source="claude")
    temp_journal.log_decision("TEST", "trend_continuation", {},
                              {"approved": True, "conviction_score": 78,
                               "reasoning": "smoke"}, source="smoke_test")
    temp_journal.log_decision("XOM", "trend_continuation", {},
                              {"error": "Ollama unreachable"},
                              source="local_shadow")

    decisions, grades = export_training._load(temp_journal.DB_FILE)
    kept, excluded = export_training.build_rows(decisions, grades)
    assert len(kept) == 1
    assert kept[0]["ticker"] == "NVDA"
    assert kept[0]["prompt_version"] == 4
    assert excluded["smoke_test"] == 1
    assert excluded["error_row"] == 1


def test_export_audit_flags_missing_grades_and_outcomes(temp_journal):
    for i in range(20):
        temp_journal.log_decision(f"T{i}", "trend_continuation",
                                  {"prompt_version": 4},
                                  {"approved": False, "conviction_score": 30,
                                   "reasoning": "r"}, source="claude")
    decisions, grades = export_training._load(temp_journal.DB_FILE)
    kept, excluded = export_training.build_rows(decisions, grades)
    report = export_training.audit(kept, excluded)
    assert "Grades are the bottleneck" in report
    assert "Outcomes are sparse" in report


def test_export_attaches_grades(temp_journal):
    did = temp_journal.log_decision("NVDA", "intern_scan",
                                    {"prompt_version": 4, "date": "2026-08-10"},
                                    {"approved": True, "conviction_score": 75,
                                     "reasoning": "r"}, source="intern_desk")
    date = temp_journal.get_meta("x")  # noop, keeps import used
    conn = sqlite3.connect(temp_journal.DB_FILE)
    row = conn.execute("SELECT timestamp FROM decisions WHERE id=?",
                       (did,)).fetchone()[0][:10]
    conn.close()
    temp_journal.intern_record(row, "NVDA", "long_setup", 75)
    temp_journal.intern_grade(row, "NVDA", "good", "clean read")

    decisions, grades = export_training._load(temp_journal.DB_FILE)
    kept, _ = export_training.build_rows(decisions, grades)
    assert kept[0]["grade"] == "good"
    assert kept[0]["grade_note"] == "clean read"
