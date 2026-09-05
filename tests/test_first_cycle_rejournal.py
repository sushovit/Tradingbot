"""
W5 (PM_PLAN.md): the first cycle of a new session re-journalled yesterday's
rules passes. No network.

CAUSE (established before fixing, three legs, all required):

 1. Before today's daily bar exists — the very first cycles after the open —
    a detector's `window.iloc[-2]` still points at the bar it judged
    yesterday. On 2026-09-04 at 09:30:37 the detectors were re-judging the
    2026-09-02 bar, the same bar they judged on 2026-09-03 at 09:37.
    Confirmed against live bars: the journaled volumes (HOOD 474379,
    APP 167350, AMGN 85483) all belong to 2026-09-02.

 2. The in-memory guards (`journaled_passes`, `daily_evaluated`) are
    per-process, and the worker restarts every session, so neither survives
    the day boundary.

 3. journal.log_rules_pass deduped per ET DAY, and it was a new day — so the
    byte-identical row was written again. journal_pass_once computed a
    bar-aware key but never passed it to the journal.

The fix is leg 3 only: the signal bar now reaches the database key. Which
bar the detectors trade on is deliberately unchanged.
"""

import sqlite3

import pandas as pd
import pytest

import daily_eval


def daily_frame(dates):
    idx = pd.to_datetime(dates)
    n = len(dates)
    return pd.DataFrame({"open": range(n), "high": range(n), "low": range(n),
                         "close": range(n), "volume": [1000] * n}, index=idx)


# ============================================================ the cause

def test_detector_bar_and_completed_bar_diverge_without_a_partial_bar():
    """The heart of it. With today's partial bar present the two agree; on
    the first cycle of a new day, before it exists, they are one bar apart —
    and the detector's bar is the one it already judged yesterday."""
    bars = daily_frame(["2026-08-31", "2026-09-01", "2026-09-02",
                        "2026-09-03"])

    # During 2026-09-03: the 09-03 bar is today's partial.
    assert str(bars.index[-2])[:10] == "2026-09-02"
    assert daily_eval.completed_bar_date(bars, "2026-09-03") == "2026-09-02"

    # First cycle of 2026-09-04: no 09-04 bar yet. The detector still reads
    # 09-02 — yesterday's judgement — while completed_bar_date has moved on.
    assert str(bars.index[-2])[:10] == "2026-09-02"
    assert daily_eval.completed_bar_date(bars, "2026-09-04") == "2026-09-03"


# ============================================================ the fix

def test_the_same_signal_bar_is_journaled_once_across_a_day_boundary(
        temp_journal):
    """The regression itself: the same bar, judged on two calendar days,
    must produce ONE row."""
    first = temp_journal.log_rules_pass(
        "HOOD", "mean_reversion_reclaim", "volume_low",
        "Reclaim volume 474379 <= 1.3x avg 571371", bar_key="2026-09-02")
    # ...next session, same bar, byte-identical details.
    second = temp_journal.log_rules_pass(
        "HOOD", "mean_reversion_reclaim", "volume_low",
        "Reclaim volume 474379 <= 1.3x avg 571371", bar_key="2026-09-02")

    assert first == second
    conn = sqlite3.connect(temp_journal.DB_FILE)
    n = conn.execute("SELECT COUNT(*) FROM decisions WHERE source='rules'"
                     ).fetchone()[0]
    conn.close()
    assert n == 1


def test_a_genuinely_new_signal_bar_still_gets_its_own_row(temp_journal):
    """The fix must not silence real information."""
    temp_journal.log_rules_pass("HOOD", "mean_reversion_reclaim",
                                "volume_low", "d", bar_key="2026-09-02")
    temp_journal.log_rules_pass("HOOD", "mean_reversion_reclaim",
                                "volume_low", "d", bar_key="2026-09-03")
    conn = sqlite3.connect(temp_journal.DB_FILE)
    n = conn.execute("SELECT COUNT(*) FROM decisions WHERE source='rules'"
                     ).fetchone()[0]
    conn.close()
    assert n == 2


def test_different_tickers_setups_and_filters_stay_separate(temp_journal):
    bar = "2026-09-02"
    temp_journal.log_rules_pass("HOOD", "mean_reversion_reclaim",
                                "volume_low", "d", bar_key=bar)
    temp_journal.log_rules_pass("APP", "mean_reversion_reclaim",
                                "volume_low", "d", bar_key=bar)
    temp_journal.log_rules_pass("HOOD", "momentum_continuation",
                                "volume_low", "d", bar_key=bar)
    temp_journal.log_rules_pass("HOOD", "mean_reversion_reclaim",
                                "adx_low", "d", bar_key=bar)
    conn = sqlite3.connect(temp_journal.DB_FILE)
    n = conn.execute("SELECT COUNT(*) FROM decisions WHERE source='rules'"
                     ).fetchone()[0]
    conn.close()
    assert n == 4


def test_session_scoped_passes_keep_the_per_day_key(temp_journal):
    """circuit_breaker and outside_hours are not about a bar; without a
    bar_key the old per-day behaviour must stand."""
    a = temp_journal.log_rules_pass("NVDA", "trend_continuation",
                                    "circuit_breaker", "daily loss")
    b = temp_journal.log_rules_pass("NVDA", "trend_continuation",
                                    "circuit_breaker", "daily loss")
    assert a == b
    conn = sqlite3.connect(temp_journal.DB_FILE)
    n = conn.execute("SELECT COUNT(*) FROM decisions WHERE source='rules'"
                     ).fetchone()[0]
    conn.close()
    assert n == 1


def test_the_signal_bar_is_stored_so_the_row_can_be_audited(temp_journal):
    temp_journal.log_rules_pass("HOOD", "mean_reversion_reclaim",
                                "volume_low", "details here",
                                bar_key="2026-09-02")
    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT context FROM decisions WHERE source='rules'"
                       ).fetchone()
    conn.close()
    import json
    context = json.loads(row["context"])
    assert context["bar"] == "2026-09-02"
    assert context["details"] == "details here"


# ============================================================ wiring

def test_the_worker_passes_the_signal_bar_to_the_journal():
    """journal_pass_once computed a bar-aware key and then dropped it. The
    in-memory set cannot help across a restart, so the database key is the
    only thing that can catch this."""
    with open("streamlit_app.py", encoding="utf-8") as f:
        body = f.read()
    idx = body.index("def journal_pass_once")
    block = body[idx:idx + 1400]
    assert "journal_rules_pass(t, setup_name, filter_name, details," in block
    assert "bar_key=bar_key" in block

    wrapper = body[body.index("def journal_rules_pass"):]
    assert "bar_key=bar_key" in wrapper[:400]


def test_which_bar_the_detectors_trade_on_is_unchanged():
    """W5's constraint: stop the duplicate rows, do not move the trade."""
    with open("streamlit_app.py", encoding="utf-8") as f:
        body = f.read()
    # daily strategies still gate on should_evaluate against the completed
    # bar, and rejection rows still key off strat_df.index[-2].
    assert "daily_eval.should_evaluate(" in body
    assert 'bar_key = str(strat_df.index[-2]) if len(strat_df) >= 2 else None' \
        in body
