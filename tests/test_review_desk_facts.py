"""
W2 (PM_PLAN.md): the review prompt carries the settled desk facts, so the
reviewer stops re-flagging them. Prompt-only — no behaviour change.

Each fact below was verified against the live journal on 2026-09-05 before
being written into the prompt; the tests pin the wording that carries the
meaning, not every word.
"""

import re

import review_bot

PROMPT = review_bot.REVIEW_SYSTEM_PROMPT


def test_block_exists_and_is_framed_as_settled():
    assert "DESK FACTS THE REVIEWER MUST NOT RE-FLAG" in PROMPT
    assert "mention one only if it CHANGES" in PROMPT


def test_block_sits_after_the_duties_and_before_the_constraint():
    """Placement matters: dropped between duties 4 and 5 it would break the
    numbered list the reviewer is following."""
    assert (PROMPT.index("5. TOMORROW")
            < PROMPT.index("DESK FACTS")
            < PROMPT.index("HARD CONSTRAINT"))


# ------------------------------------------------------------ (a) floor

def test_fact_a_breakeven_floor_is_the_rule_not_a_violation():
    assert "max(ATR trail, entry price)" in PROMPT
    assert "+1R" in PROMPT
    assert "NOT a Rule 1 violation" in PROMPT


def test_fact_a_demands_stop_distance_measured_from_entry():
    """The reviewer kept reporting a breakeven stop as 'miles below price',
    which is true and useless — the risk is zero either way."""
    assert re.search(r"measure it from ENTRY, not from the\s+current price",
                     PROMPT)


# ------------------------------------------------------------ (b) shutdown

def test_fact_b_states_the_shutdown_time_and_the_expected_heartbeat():
    assert "16:15 ET" in PROMPT
    assert "900 s" in PROMPT
    assert "EXPECTED post-shutdown state" in PROMPT


def test_fact_b_still_leaves_in_session_stalls_flaggable():
    """The exemption must not switch off heartbeat monitoring altogether."""
    assert "Only a stale heartbeat DURING the session is an anomaly" in PROMPT


# ------------------------------------------------------------ (c) double fills

def test_fact_c_names_both_tickers_the_date_and_the_cause():
    assert "NOK and ORCL, 2026-08-13" in PROMPT
    assert "REAL double fills" in PROMPT
    assert "distinct broker order id" in PROMPT
    assert "Cumulative PnL is correct" in PROMPT


def test_fact_c_matches_what_the_journal_actually_holds():
    """Guard against the prompt asserting something the ledger contradicts:
    two BUYs and two SELLs per ticker, every leg a distinct order id."""
    import sqlite3
    conn = sqlite3.connect(review_bot.journal.DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ticker, action, qty, broker_order_id FROM trades "
        "WHERE ticker IN ('NOK','ORCL') AND timestamp LIKE '2026-08-13%'"
    ).fetchall()
    conn.close()
    if not rows:
        return                      # fresh/temp DB — nothing to contradict
    for ticker in ("NOK", "ORCL"):
        legs = [r for r in rows if r["ticker"] == ticker]
        buys = [r for r in legs if r["action"] == "BUY"]
        assert len(buys) == 2, f"{ticker}: prompt claims a double fill"
        ids = [r["broker_order_id"] for r in legs]
        assert len(set(ids)) == len(ids), f"{ticker}: order ids must differ"
    assert "NOK 28 sh twice" in PROMPT and "ORCL 1 sh twice" in PROMPT


# ------------------------------------------------------------ (d) shadow

def test_fact_d_records_zero_approvals_and_the_known_error_rate():
    assert "approved 0 of" in PROMPT
    assert "~275" in PROMPT
    assert "~11% error rate" in PROMPT
    assert "ADVISORY and non-blocking" in PROMPT


def test_fact_d_is_dated_so_it_reads_as_a_snapshot_not_a_law():
    """The count drifts every session. Dating it stops the prompt asserting a
    stale number as present truth."""
    assert "as of 2026-09-05" in PROMPT


def test_fact_d_still_asks_for_an_alert_if_it_changes():
    assert "Raise it only if" in PROMPT
    assert "a shadow APPROVAL appears" in PROMPT


# ------------------------------------------------------------ no behaviour change

def test_the_reviewer_is_still_read_only():
    assert "HARD CONSTRAINT — YOU ARE READ-ONLY" in PROMPT
    assert "no entry/stop/target order sheets" in PROMPT


def test_the_original_duties_are_all_intact():
    for duty in ("1. MARK THE BOOK", "2. GRADE THE DAY'S DECISIONS",
                 "3. FLAG ANOMALIES", "4. GRADE EVERY PROBATION TRADE",
                 "5. TOMORROW'S WATCH ITEMS"):
        assert duty in PROMPT
