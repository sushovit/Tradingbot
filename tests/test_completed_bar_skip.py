"""
W6 (PM_PLAN.md): a completed daily bar could be skipped entirely. No network.

THE FAULT. Detectors read iloc[-2] as the signal bar and iloc[-1] as today's
partial. On the first cycles of a session, before today's bar exists, that
is off by one: iloc[-2] is the bar judged YESTERDAY and iloc[-1] is the real
completed bar. Evaluating then did two wrong things at once —

  * it re-judged yesterday's bar (the 2026-09-04 09:30:37 duplicate rows:
    HOOD 474379, APP 167350, AMGN 85483, every one a 2026-09-02 volume), and
  * mark_evaluated recorded TODAY's completed bar as done, so when the
    partial bar appeared minutes later the genuinely new bar was skipped and
    never judged at all.

THE FIX is one condition: hold off until index[-1] is today. No detector
logic, bar selection or threshold is touched.
"""

import pandas as pd
import pytest

import daily_eval

TODAY = "2026-09-04"
YESTERDAY = "2026-09-03"


def frame(dates):
    """Daily OHLCV whose bars fall on `dates`."""
    idx = pd.to_datetime(dates)
    rows = [(100.0, 102.0, 98.0, 101.0, 1e6)] * len(dates)
    return pd.DataFrame(rows, index=idx,
                        columns=["open", "high", "low", "close", "volume"])


BEFORE_OPEN = ["2026-08-31", "2026-09-01", "2026-09-02", YESTERDAY]
AFTER_OPEN = BEFORE_OPEN + [TODAY]


# ============================================ (1) no partial bar -> no eval

def test_not_evaluated_before_todays_bar_exists():
    """First cycles of the session: the detector's iloc[-2] still points at
    2026-09-02, a bar judged yesterday. Nothing should run."""
    evaluated = {}
    df = frame(BEFORE_OPEN)
    assert str(df.index[-1])[:10] == YESTERDAY
    assert str(df.index[-2])[:10] == "2026-09-02"      # yesterday's judgement

    for _ in range(5):                                  # several cycles
        assert daily_eval.should_evaluate(
            evaluated, "HOOD", "mean_reversion_reclaim", df, TODAY) is False
    assert evaluated == {}                              # nothing recorded


def test_nothing_is_journaled_while_waiting(temp_journal, monkeypatch):
    """The duplicate rows were the visible symptom; with no evaluation there
    is nothing to journal."""
    import sqlite3
    evaluated = {}
    df = frame(BEFORE_OPEN)
    for _ in range(3):
        if daily_eval.should_evaluate(evaluated, "HOOD",
                                      "mean_reversion_reclaim", df, TODAY):
            temp_journal.log_rules_pass(
                "HOOD", "mean_reversion_reclaim", "volume_low",
                "Reclaim volume 474379 <= 1.3x avg 571371",
                bar_key=str(df.index[-2])[:10])
    conn = sqlite3.connect(temp_journal.DB_FILE)
    n = conn.execute("SELECT COUNT(*) FROM decisions WHERE source='rules'"
                     ).fetchone()[0]
    conn.close()
    assert n == 0


# ============================================ (2) partial bar -> one eval

def test_evaluated_exactly_once_on_yesterdays_bar_once_the_partial_appears():
    evaluated = {}
    df = frame(AFTER_OPEN)
    assert str(df.index[-1])[:10] == TODAY              # partial bar present
    assert str(df.index[-2])[:10] == YESTERDAY          # the bar to judge

    assert daily_eval.should_evaluate(
        evaluated, "HOOD", "mean_reversion_reclaim", df, TODAY) is True
    daily_eval.mark_evaluated(evaluated, "HOOD", "mean_reversion_reclaim",
                              df, TODAY)

    # ...and never again for that bar.
    for _ in range(5):
        assert daily_eval.should_evaluate(
            evaluated, "HOOD", "mean_reversion_reclaim", df, TODAY) is False

    # It is YESTERDAY's bar that was recorded, not today's partial.
    assert evaluated[("HOOD", "mean_reversion_reclaim")] == YESTERDAY


def test_the_full_session_sequence_judges_each_bar_once():
    """The regression, end to end: wait, then judge, and do not lose the bar.
    Before the fix this sequence judged 09-02 twice and 09-03 never."""
    evaluated = {}
    judged = []

    for df in (frame(BEFORE_OPEN), frame(BEFORE_OPEN),   # pre-bar cycles
               frame(AFTER_OPEN), frame(AFTER_OPEN),     # bar has appeared
               frame(AFTER_OPEN)):
        if daily_eval.should_evaluate(evaluated, "HOOD",
                                      "mean_reversion_reclaim", df, TODAY):
            judged.append(str(df.index[-2])[:10])
            daily_eval.mark_evaluated(evaluated, "HOOD",
                                      "mean_reversion_reclaim", df, TODAY)

    assert judged == [YESTERDAY]          # once, and on the right bar
    assert "2026-09-02" not in judged     # yesterday's bar is not re-judged


# ============================================ Rule #3 reads the real open

def test_rule_3_now_sees_the_true_session_open():
    """session_open_price falls back to current_price when today's bar is
    missing. Evaluating only once the bar exists means the gap-abort reads
    the actual open instead of that fallback."""
    df = frame(AFTER_OPEN)
    assert daily_eval.session_open_price(df, 999.0, TODAY) == \
        pytest.approx(float(df["open"].iloc[-1]))

    stale = frame(BEFORE_OPEN)
    assert daily_eval.session_open_price(stale, 999.0, TODAY) == 999.0
    # ...and that fallback state is now never evaluated.
    assert daily_eval.should_evaluate({}, "HOOD", "mean_reversion_reclaim",
                                      stale, TODAY) is False


# ============================================ edges

def test_a_new_session_rearms_the_evaluation():
    evaluated = {}
    df = frame(AFTER_OPEN)
    daily_eval.mark_evaluated(evaluated, "HOOD", "mean_reversion_reclaim",
                              df, TODAY)
    assert daily_eval.should_evaluate(
        evaluated, "HOOD", "mean_reversion_reclaim", df, TODAY) is False

    tomorrow = "2026-09-08"
    df2 = frame(AFTER_OPEN + [tomorrow])
    assert daily_eval.should_evaluate(
        evaluated, "HOOD", "mean_reversion_reclaim", df2, tomorrow) is True


def test_empty_and_missing_frames_are_still_safe():
    assert daily_eval.should_evaluate({}, "X", "s", None, TODAY) is False
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    assert daily_eval.should_evaluate({}, "X", "s", empty, TODAY) is False


def test_a_single_bar_frame_does_not_evaluate():
    """One bar, and it is today's partial: there is no completed bar to
    judge."""
    df = frame([TODAY])
    assert daily_eval.should_evaluate({}, "X", "s", df, TODAY) is False


def test_intraday_strategies_are_untouched():
    """trend_continuation never went through this gate; the fix must not
    reach it."""
    with open("bot_config.json", encoding="utf-8") as f:
        import json
        config = json.load(f)
    assert daily_eval.strategy_timeframe(
        "trend_continuation", config, "intraday") == "intraday"


def test_completed_bar_date_is_unchanged():
    """The fix is a gate in should_evaluate, not a change to which bar counts
    as completed."""
    assert daily_eval.completed_bar_date(frame(AFTER_OPEN), TODAY) == YESTERDAY
    assert daily_eval.completed_bar_date(frame(BEFORE_OPEN), TODAY) == YESTERDAY
