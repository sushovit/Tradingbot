"""
S3 (PM_PLAN.md / agenda 10.11): the rejection-outcome tracker. No network —
bars are injected.

The question it exists to answer: of the trades the desk DECLINED, how many
would have reached target, how many would have stopped, and how does that
break down by which gate declined them. Over months that is how a boardroom
decides which gate is earning its keep, instead of arguing about it.

Two facts about the data shape this:
  * only ~8% of rejections carry entry/stop/target — a deterministic filter
    rejects BEFORE geometry exists, so those rows can never be replayed;
  * the gatekeeper journals a SENTENCE as its reason, and 121 rejections
    journal none at all, so the raw reason is not groupable.
"""

import sqlite3

import pandas as pd
import pytest

import outcomes


def bars(rows, start="2026-09-01"):
    """Daily OHLC from (open, high, low, close) tuples."""
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"],
                        index=idx)


# ============================================================ replay

def test_target_is_recorded_with_the_r_it_achieved():
    # entry 100, stop 90 -> 1R = 10; target 130 = +3R.
    frame = bars([(100, 101, 99, 100),      # 09-01, the decision day
                  (101, 105, 100, 104),
                  (104, 131, 103, 130)])    # reaches 130
    outcome, r, used, resolved = outcomes.replay(
        frame, "2026-09-01", 100.0, 90.0, 130.0)
    assert outcome == "target"
    assert r == pytest.approx(3.0)
    assert used == 2 and resolved is True


def test_stop_is_recorded_as_minus_one_r():
    frame = bars([(100, 101, 99, 100),
                  (100, 101, 89, 92)])      # trades through 90
    outcome, r, used, resolved = outcomes.replay(
        frame, "2026-09-01", 100.0, 90.0, 130.0)
    assert outcome == "stop"
    assert r == pytest.approx(-1.0)
    assert resolved is True


def test_a_bar_spanning_both_levels_is_assumed_to_stop_first():
    """The conservative convention backtest.simulate_bracket uses; an
    outcome here has to be comparable with the study tables."""
    frame = bars([(100, 101, 99, 100),
                  (100, 135, 88, 120)])     # hits both
    outcome, _, _, _ = outcomes.replay(frame, "2026-09-01", 100.0, 90.0,
                                       130.0)
    assert outcome == "stop"


def test_a_gap_through_a_level_fills_at_the_open():
    frame = bars([(100, 101, 99, 100),
                  (85, 95, 84, 94)])        # opens below the stop
    outcome, r, _, _ = outcomes.replay(frame, "2026-09-01", 100.0, 90.0,
                                       130.0)
    assert outcome == "stop"
    assert r == pytest.approx(-1.5)         # filled at 85, not 90


def test_neither_reports_where_it_stood_and_resolves_at_the_horizon():
    flat = [(100, 101, 99, 100)] + [(100, 102, 98, 101)] * 10
    outcome, r, used, resolved = outcomes.replay(
        bars(flat), "2026-09-01", 100.0, 90.0, 130.0, horizon=10)
    assert outcome == "neither"
    assert r == pytest.approx(0.1)          # last close 101 -> +0.1R
    assert used == 10 and resolved is True


def test_a_rejection_does_not_resolve_on_the_night_it_is_made():
    """Today's rejection has zero forward sessions. Running nightly is what
    makes this correct, not a single pass."""
    frame = bars([(100, 101, 99, 100)])
    outcome, r, used, resolved = outcomes.replay(
        frame, "2026-09-01", 100.0, 90.0, 130.0)
    assert (outcome, r, used, resolved) == ("neither", None, 0, False)


def test_the_horizon_is_ten_sessions():
    assert outcomes.HORIZON_SESSIONS == 10
    # A target reached on session 11 must NOT count.
    rows = [(100, 101, 99, 100)] + [(100, 102, 98, 100)] * 10 + \
           [(100, 140, 99, 139)]
    outcome, _, used, _ = outcomes.replay(bars(rows), "2026-09-01",
                                          100.0, 90.0, 130.0)
    assert outcome == "neither" and used == 10


# ============================================================ geometry

def test_geometry_is_read_from_a_gatekeeper_row():
    ctx = '{"setup": "reclaim", "entry": 108.82, "stop": 104.88, ' \
          '"target": 120.64}'
    assert outcomes.geometry(ctx) == (108.82, 104.88, 120.64)


def test_geometry_is_recovered_from_an_enriched_size_zero_row():
    """Those carry entry/stop in free text and no target — a rejection that
    never got that far. The 3R floor makes the replay comparable."""
    ctx = '{"details": "entry=253.76 stop=231.56 stop_distance_usd=22.20"}'
    entry, stop, target = outcomes.geometry(ctx)
    assert (entry, stop) == (253.76, 231.56)
    assert target == pytest.approx(253.76 + 3 * (253.76 - 231.56))


def test_a_deterministic_filter_row_has_no_geometry():
    """This is the coverage gap, and it is the point: an adx_low rejection
    happens BEFORE geometry exists."""
    assert outcomes.geometry('{"details": "ADX 13.0 < 30", "bar": "x"}') is None
    assert outcomes.geometry(None) is None
    assert outcomes.geometry("not json") is None


def test_impossible_geometry_is_refused():
    assert outcomes.geometry('{"entry": 100, "stop": 110, "target": 130}') \
        is None                                  # stop above entry
    assert outcomes.geometry('{"entry": 100, "stop": 90, "target": 95}') \
        is None                                  # target below entry


# ============================================================ reason keys

def test_a_deterministic_reason_is_already_a_key():
    assert outcomes.reason_key("rules", "adx_low") == "adx_low"
    assert outcomes.reason_key("rules", "volume_low") == "volume_low"


def test_gatekeeper_prose_is_bucketed_by_what_it_cites():
    """Grouping on the raw sentence would give ~180 one-off rows, which is
    not a table."""
    assert outcomes.reason_key(
        "claude", "ADX at 19.9 is below the 25 threshold") == "gatekeeper_adx"
    assert outcomes.reason_key(
        "claude", "RSI 44.9 is below 50") == "gatekeeper_rsi"
    assert outcomes.reason_key(
        "claude", "Volume trend is declining") == "gatekeeper_volume"
    assert outcomes.reason_key(
        "claude", "little room to resistance") == "gatekeeper_resistance"


def test_an_unstated_gatekeeper_reason_still_groups():
    """121 gatekeeper rejections journal no reason at all."""
    assert outcomes.reason_key("claude", None) == "gatekeeper_unstated"
    assert outcomes.reason_key("claude", "") == "gatekeeper_unstated"
    assert outcomes.reason_key("claude", "just did not like it") == \
        "gatekeeper_other"


# ============================================================ end to end

@pytest.fixture
def three_rejections(temp_journal):
    """The fixture the order asks for: three rejections with known bars —
    one that would have hit target, one that would have stopped, and one a
    deterministic filter rejected before geometry existed."""
    win = temp_journal.log_decision(
        "WINR", "mean_reversion_reclaim",
        {"entry": 100.0, "stop": 90.0, "target": 130.0},
        {"approved": False, "rejection_reason": "ADX at 19.9 is below 25"},
        source="claude")
    lose = temp_journal.log_decision(
        "LOSR", "mean_reversion_reclaim",
        {"entry": 50.0, "stop": 45.0, "target": 65.0},
        {"approved": False, "rejection_reason": "RSI 44 below 50"},
        source="claude")
    blind = temp_journal.log_decision(
        "BLND", "trend_continuation", {"details": "ADX 13.0 < 30"},
        {"approved": False, "rejection_reason": "adx_low"}, source="rules")

    decided = temp_journal._today_et()
    start = pd.Timestamp(decided)
    def frame(rows):
        idx = pd.bdate_range(start, periods=len(rows))
        return pd.DataFrame(rows, columns=["open", "high", "low", "close"],
                            index=idx)

    injected = {
        "WINR": frame([(100, 101, 99, 100)] + [(101, 131, 100, 130)]),
        "LOSR": frame([(50, 51, 49, 50)] + [(50, 51, 44, 44.5)]),
        "BLND": frame([(10, 11, 9, 10)] * 3),
    }
    return temp_journal, injected, {"win": win, "lose": lose, "blind": blind}


def test_the_three_rejections_are_classified_correctly(three_rejections):
    jnl, injected, ids = three_rejections
    result = outcomes.evaluate(jnl._today_et(), bars_by_ticker=injected,
                               db_file=jnl.DB_FILE)

    assert result["evaluated"] == 3
    assert result["by_outcome"] == {"target": 1, "stop": 1, "no_geometry": 1}

    conn = sqlite3.connect(jnl.DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = {r["ticker"]: dict(r) for r in
            conn.execute("SELECT * FROM rejection_outcomes")}
    conn.close()

    assert rows["WINR"]["outcome"] == "target"
    assert rows["WINR"]["r_achieved"] == pytest.approx(3.0)
    assert rows["WINR"]["reason_key"] == "gatekeeper_adx"

    assert rows["LOSR"]["outcome"] == "stop"
    # Opens at 50 (above the stop) and trades down through 45 intraday, so
    # it fills AT the stop, not at the open: a clean -1R.
    assert rows["LOSR"]["r_achieved"] == pytest.approx(-1.0)

    assert rows["BLND"]["outcome"] == "no_geometry"
    assert rows["BLND"]["reason_key"] == "adx_low"
    assert rows["BLND"]["resolved"] == 1       # it can never resolve otherwise


def test_evaluation_is_idempotent(three_rejections):
    jnl, injected, _ = three_rejections
    for _ in range(3):
        outcomes.evaluate(jnl._today_et(), bars_by_ticker=injected,
                          db_file=jnl.DB_FILE)
    conn = sqlite3.connect(jnl.DB_FILE)
    n = conn.execute("SELECT COUNT(*) FROM rejection_outcomes").fetchone()[0]
    conn.close()
    assert n == 3                              # upsert, not append


def test_an_unresolved_row_is_re_evaluated_on_a_later_run(three_rejections):
    """A rejection resolves over time. The first night sees no forward bars;
    a later night must pick it up without being asked for that date again."""
    jnl, injected, _ = three_rejections
    today = jnl._today_et()

    # Night one: no forward bars at all for WINR.
    only_today = {"WINR": injected["WINR"].iloc[:1]}
    outcomes.evaluate(today, bars_by_ticker=only_today, db_file=jnl.DB_FILE)
    conn = sqlite3.connect(jnl.DB_FILE)
    row = conn.execute("SELECT outcome, resolved FROM rejection_outcomes "
                       "WHERE ticker='WINR'").fetchone()
    conn.close()
    assert row == ("neither", 0)

    # Night two: asked for a DIFFERENT date; the unresolved row is swept up.
    outcomes.evaluate("1999-01-01", bars_by_ticker=injected,
                      db_file=jnl.DB_FILE)
    conn = sqlite3.connect(jnl.DB_FILE)
    row = conn.execute("SELECT outcome, resolved FROM rejection_outcomes "
                       "WHERE ticker='WINR'").fetchone()
    conn.close()
    assert row == ("target", 1)


def test_approved_decisions_are_never_evaluated(temp_journal):
    temp_journal.log_decision("YES", "reclaim",
                              {"entry": 100.0, "stop": 90.0, "target": 130.0},
                              {"approved": True, "conviction_score": 80,
                               "reasoning": "good"}, source="claude")
    result = outcomes.evaluate(temp_journal._today_et(), bars_by_ticker={},
                               db_file=temp_journal.DB_FILE)
    assert result["evaluated"] == 0


def test_the_monthly_table_groups_reason_by_outcome(three_rejections):
    jnl, injected, _ = three_rejections
    outcomes.evaluate(jnl._today_et(), bars_by_ticker=injected,
                      db_file=jnl.DB_FILE)
    table = outcomes.monthly_table(jnl._today_et()[:7], db_file=jnl.DB_FILE)
    pairs = {(r["reason_key"], r["outcome"]): r for r in table}
    assert pairs[("gatekeeper_adx", "target")]["count"] == 1
    assert pairs[("gatekeeper_adx", "target")]["avg_r"] == pytest.approx(3.0)
    assert pairs[("adx_low", "no_geometry")]["count"] == 1
    assert pairs[("adx_low", "no_geometry")]["avg_r"] is None


# ============================================================ safety

def test_the_tracker_places_no_orders_and_touches_no_position_state():
    """S3 is explicitly 'no trading behaviour changes'."""
    with open("outcomes.py", encoding="utf-8") as f:
        body = f.read()
    for forbidden in ("submit_bracket", "close_position", "replace_stop",
                      "write_positions", "log_trade", "positions.json"):
        assert forbidden not in body, forbidden


def test_outcomes_live_in_their_own_table(three_rejections):
    """Hypothetical fills in `trades` would corrupt realised PnL; in
    `decisions` they would enter the training export."""
    jnl, injected, _ = three_rejections
    before = jnl.decision_counts()["total"]
    outcomes.evaluate(jnl._today_et(), bars_by_ticker=injected,
                      db_file=jnl.DB_FILE)
    assert jnl.decision_counts()["total"] == before
    conn = sqlite3.connect(jnl.DB_FILE)
    n = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    conn.close()
    assert n == 0


def test_the_nightly_job_runs_after_the_shutdown():
    with open("jobs/outcomes.bat", encoding="utf-8") as f:
        body = f.read()
    assert "outcomes.py" in body
    assert "PYTHONUTF8=1" in body
    assert "logs\\outcomes.log" in body


# ============================================ intraday guard + median

def test_an_intraday_setup_is_not_replayed_on_daily_bars():
    """trend_continuation stops are 5-minute-scale. RKLB 2026-09-09 was
    entry 64.27 / stop 64.19 — a 1R of EIGHT CENTS. Replaying that against
    daily bars produced -32.9R and poisoned the averages."""
    assert outcomes.is_daily_setup("trend_continuation") is False
    for setup in ("mean_reversion_reclaim", "momentum_continuation",
                  "pullback_in_uptrend", "post_earnings_continuation"):
        assert outcomes.is_daily_setup(setup) is True


def test_an_intraday_rejection_lands_in_no_geometry(temp_journal):
    did = temp_journal.log_decision(
        "RKLB", "trend_continuation",
        {"entry": 64.27, "stop": 64.19, "target": 64.51},
        {"approved": False, "rejection_reason": "ADX below threshold"},
        source="claude")
    frame = bars([(64, 65, 63, 64)] * 4,
                 start=temp_journal._today_et())
    result = outcomes.evaluate(temp_journal._today_et(),
                               bars_by_ticker={"RKLB": frame},
                               db_file=temp_journal.DB_FILE)
    assert result["by_outcome"] == {"no_geometry": 1}


def test_the_median_is_reported_beside_the_mean(temp_journal):
    """A single pathological stop must not silently own the average."""
    frame_start = temp_journal._today_et()
    injected = {}
    # AAA and BBB stop cleanly at -1R. CCC has a 10-cent stop and GAPS far
    # through it, which is the RKLB shape: a real row with an absurd R.
    spec = (("AAA", 90.0, (100, 101, 89.0, 90.0)),
            ("BBB", 90.0, (100, 101, 89.0, 90.0)),
            ("CCC", 99.9, (1.0, 2.0, 0.5, 1.0)))
    for tkr, stop, second_bar in spec:
        temp_journal.log_decision(
            tkr, "mean_reversion_reclaim",
            {"entry": 100.0, "stop": stop, "target": 130.0},
            {"approved": False, "rejection_reason": "ADX low"},
            source="claude")
        injected[tkr] = bars([(100, 101, 99.95, 100), second_bar],
                             start=frame_start)
    outcomes.evaluate(frame_start, bars_by_ticker=injected,
                      db_file=temp_journal.DB_FILE)
    row = [r for r in outcomes.monthly_table(frame_start[:7],
                                             db_file=temp_journal.DB_FILE)
           if r["outcome"] == "stop"][0]
    assert row["count"] == 3
    # CCC gapped through a 10-cent stop: -990R, which owns the mean.
    assert row["avg_r"] < row["median_r"]
    assert row["median_r"] == pytest.approx(-1.0)
