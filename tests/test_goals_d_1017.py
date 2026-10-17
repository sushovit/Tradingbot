"""
CEO 2026-10-17 (D): goals.py
  - profit->loss goal counts trades that were up >= +1R (was +0.5R)
  - exit-rule text comes from config (the live R/ATR ladder)
  - cause analysis reads ops events before proposing fixes
"""

import json
import sqlite3
from collections import Counter

import goals


def test_profit_to_loss_threshold_is_one_r():
    assert goals.GIVEBACK_MIN_R == 1.0
    assert "+1R open, closed red" in goals.DEFINITIONS


def test_exit_rule_text_tracks_the_config():
    cfg = json.load(open("bot_config.json", encoding="utf-8"))
    text = goals.exit_rule_text(cfg)
    assert text.startswith("R/ATR ladder: +1R -> entry, +1.5R -> entry, "
                           "+2R -> entry +1R")
    assert "2.5xATR14" in text and "day 5" in text and "day 10" in text
    ladder = {"risk_profiles": {"Moderate": {"daily_trailing": "ladder",
                                             "daily_lock": {"start_pct": 0.5,
                                                            "cushion_pct": 0.5}}}}
    assert goals.exit_rule_text(ladder).startswith("owner ladder")


def ops_db(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY, timestamp TEXT,"
                 " ticker TEXT, setup_name TEXT, source TEXT)")
    for ts, kind in rows:
        conn.execute("INSERT INTO decisions (timestamp, ticker, setup_name, source)"
                     " VALUES (?,?,?,?)", (ts, "OPS", kind, "ops"))
    conn.execute("INSERT INTO decisions (timestamp, ticker, setup_name, source)"
                 " VALUES ('2026-10-14 10:00:00','X','network_unavailable','claude')")
    return conn


def test_ops_events_counts_only_ops_rows_in_the_period():
    conn = ops_db([("2026-10-13 09:25:00", "network_unavailable"),
                   ("2026-10-13 09:30:00", "network_unavailable"),
                   ("2026-10-14 09:31:00", "target_capped"),
                   ("2026-10-14 10:00:00", "entry_reconciled"),
                   ("2026-10-20 10:00:00", "process_suspended"),       # outside
                   ("2026-10-14 10:00:00", "daily_bars_unavailable")])  # not read
    c = goals.ops_events(conn, "2026-10-12", "2026-10-16")
    assert c == Counter({"network_unavailable": 2, "target_capped": 1,
                         "entry_reconciled": 1})


def ev(ops, **over):
    base = {"funnel": {"signals": 0, "rule_rejections": Counter(),
                       "approvals": 0, "gatekeeper_blocks": Counter(),
                       "skipped_after_approval": Counter()},
            "sessions": ["2026-10-13"], "opened": [], "closed": [], "wins": [],
            "realized": 0.0, "open_pnl": 0.0, "captured": 0.0,
            "peak_total": 10.0, "givebacks": [], "late": ["2026-10-13"],
            "missed": [], "starts": {"2026-10-13": "09:55"},
            "exits": Counter(), "ops": Counter(ops)}
    base.update(over)
    return base


def test_on_time_miss_with_network_failures_fixes_the_network_first():
    why, fix = goals.why_and_fix("on_time_sessions_pct",
                                 ev({"network_unavailable": 3}), {})
    assert "Ops: network_unavailable 3." in why
    assert fix.startswith("operational first: 3 network failure(s)")


def test_corrected_fills_come_before_a_pnl_strategy_reading():
    why, fix = goals.why_and_fix("monthly_pnl_usd", ev({"entry_reconciled": 2}), {})
    assert "entry_reconciled 2" in why
    assert fix.startswith("check fill accuracy first: 2 entry fill(s)")


def test_capped_targets_are_examined_before_the_exit_rule():
    why, fix = goals.why_and_fix("capture_ratio_pct", ev({"target_capped": 4}), {})
    assert "target_capped 4" in why
    assert fix.startswith("4 target(s) were capped at the ATR cap")
    assert "R/ATR ladder" in fix


def test_without_ops_events_the_rule_fix_names_the_live_rule():
    _, fix = goals.why_and_fix("capture_ratio_pct", ev({}), {})
    assert "R/ATR ladder: +1R -> entry," in fix
    _, fix = goals.why_and_fix("max_profit_to_loss_trades", ev({}), {})
    assert "live exit rule (R/ATR ladder" in fix
    assert "+0.5R" not in fix.split("(")[0]
