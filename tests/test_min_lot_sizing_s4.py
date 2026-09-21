"""
S4 (PM_PLAN.md / agenda 10.1): the capital cap rises to $5,000 and whole-share
sizing gets a minimum-lot tolerance. No network.

WHY. Whole shares floor: a $50 budget against a $57 stop distance is zero
shares, the order is never placed, and the setup is scored as if it never
fired. Two months of size_zero rows were the evidence for the cap — but the
cap alone does not fix the band immediately above the floor, it only moves
it. The tolerance admits the single share when its risk is within 15% of the
budget, and the BUY row carries risk_pct_actual so the overspend is a
measured fact rather than a silent one.

THE CAP IS NOT LIVE YET. capital_cap_usd is a ceiling, not a floor:
effective_equity takes the MIN of broker equity and the cap, so at $1,977 of
paper equity these numbers change nothing until the owner resets the account
at Alpaca (while flat). What bites immediately is max_positions 3 -> 4 and
the 4% breaker.
"""

import json
import sqlite3

import pytest

import risk


CONFIG = json.load(open("bot_config.json", encoding="utf-8"))


# ============================================ the ratified config

def test_the_five_ratified_values_are_in_the_config():
    """Item 10 ratified these five numbers on 2026-09-20. The test is the
    receipt: a later edit that quietly walks one back fails here."""
    assert CONFIG["capital_cap_usd"] == 5000
    assert CONFIG["universe"]["max_price"] == 1200
    assert CONFIG["risk_profiles"]["Moderate"]["risk_per_trade_pct"] == 1.0
    assert CONFIG["max_positions"] == 4
    assert CONFIG["daily_loss_limit_pct"] == 4.0


def test_the_tolerance_is_configured_at_fifteen_percent():
    assert CONFIG["min_lot_tolerance"] == 0.15
    assert risk.min_lot_tolerance(CONFIG) == 0.15


def test_nothing_item_ten_did_not_ratify_moved():
    """10.6 (exit rule) is HELD, and the notional cap was never in scope."""
    assert CONFIG["max_position_pct"] == 0.25
    assert CONFIG["claude_conviction_threshold"] == 70


def test_the_cap_is_a_ceiling_not_a_floor():
    """The reason Monday looks unchanged: effective equity is the MIN."""
    assert risk.effective_equity(1977.76, CONFIG) == 1977.76
    assert risk.effective_equity(97000.0, CONFIG) == 5000.0


# ============================================ the ARM case

def test_arm_sizes_three_shares_at_the_new_cap():
    """The order's worked example. $5,000 x 1% = $50 against a $15.27 stop
    distance is 3 whole shares; the 25% notional cap ($1,250) would allow 4,
    so risk — not notional — is what binds."""
    qty = risk.position_size(5000, 1.0, 270.11, 254.84,
                             position_cap_pct=0.25,
                             min_lot_tolerance=0.15)
    assert qty == 3
    assert 3 * 270.11 <= 5000 * 0.25


def test_arm_needed_no_tolerance_to_get_there():
    """3 shares is ordinary arithmetic. If this ever disagrees with the test
    above, the tolerance is silently doing work it should not be doing."""
    assert risk.position_size(5000, 1.0, 270.11, 254.84,
                              position_cap_pct=0.25) == 3


def test_arm_was_a_size_zero_under_the_old_cap():
    """What actually changed. $2,000 x 0.75% = $15 could not afford one
    $15.27 share, so ARM was journaled as size_zero and never scored."""
    assert risk.position_size(2000, 0.75, 270.11, 254.84,
                              position_cap_pct=0.25) == 0


def test_arm_journals_its_real_risk():
    """3 x $15.27 = $45.81 on $5,000 — under budget, and the row says so."""
    assert risk.actual_risk_pct(5000, 3, 270.11, 254.84) == 0.9162


# ============================================ the tolerance boundary

def test_one_share_exactly_at_the_boundary_is_allowed():
    """1.15x the budget is INSIDE a 15% tolerance. Stated as a ratio it must
    admit the share whose risk IS 1.15x — binary floats put 50 * 1.15 a hair
    below $57.50, which is why the comparison carries an epsilon."""
    assert 100 - 42.50 == 57.50                 # exactly 1.15 x $50
    assert risk.position_size(5000, 1.0, 100.0, 42.50,
                              min_lot_tolerance=0.15) == 1


def test_one_cent_past_the_boundary_is_refused():
    assert risk.position_size(5000, 1.0, 100.0, 42.49,
                              min_lot_tolerance=0.15) == 0


def test_a_six_hundred_dollar_stop_distance_still_sizes_zero():
    """The tolerance buys back the band just above the floor, not the floor
    itself. A $600 stop against a $50 budget is 12x — no share."""
    assert risk.position_size(5000, 1.0, 700.0, 100.0,
                              position_cap_pct=0.25,
                              min_lot_tolerance=0.15) == 0


def test_the_tolerance_is_off_unless_asked_for():
    """An absent config key must never widen risk by accident."""
    assert risk.position_size(5000, 1.0, 100.0, 42.50) == 0
    assert risk.min_lot_tolerance({}) == 0.0
    assert risk.min_lot_tolerance(None) == 0.0


@pytest.mark.parametrize("value", ["wide", None, -0.1, 1.5])
def test_a_bad_tolerance_reads_as_no_tolerance(value):
    assert risk.min_lot_tolerance({"min_lot_tolerance": value}) == 0.0


def test_the_tolerance_never_beats_the_notional_cap():
    """A $2,000 share with a $55 stop is inside the risk tolerance and far
    outside a $1,250 position cap. Risk tolerance is not a cash loan."""
    assert risk.position_size(5000, 1.0, 2000.0, 1945.0,
                              position_cap_pct=0.25,
                              min_lot_tolerance=0.15) == 0


def test_the_tolerance_never_beats_the_no_margin_rule():
    """Nearly all the cash is already deployed."""
    assert risk.position_size(5000, 1.0, 100.0, 42.50,
                              open_notional_usd=4950.0,
                              min_lot_tolerance=0.15) == 0


def test_the_tolerance_does_not_touch_an_affordable_signal():
    """It only ever fires below one whole share — it can never round 3 up."""
    for tol in (0.0, 0.15, 0.5):
        assert risk.position_size(5000, 1.0, 270.11, 254.84,
                                  position_cap_pct=0.25,
                                  min_lot_tolerance=tol) == 3


def test_invalid_geometry_is_still_zero_whatever_the_tolerance():
    assert risk.position_size(5000, 1.0, 100.0, 100.0,
                              min_lot_tolerance=0.15) == 0
    assert risk.position_size(5000, 1.0, 100.0, 105.0,
                              min_lot_tolerance=0.15) == 0
    assert risk.position_size(0, 1.0, 100.0, 42.50,
                              min_lot_tolerance=0.15) == 0


def test_a_tolerance_trade_still_passes_the_risk_gate():
    """Sizing is not approval. The over-budget share must still clear
    check_signal, or the tolerance would be a bypass."""
    qty = risk.position_size(5000, 1.0, 100.0, 42.50,
                             position_cap_pct=0.25, min_lot_tolerance=0.15)
    ok, reason = risk.check_signal(100.0, 42.50, 200.0, 5000,
                                   notional_usd=qty * 100.0,
                                   max_positions=4,
                                   position_cap_pct=0.25)
    assert ok is True, reason


# ============================================ risk_pct_actual

def test_the_overspend_is_reported_not_hidden():
    """The whole point of journaling it: this trade risks 1.15%, not the
    configured 1.0%, and a month of them must not read back as 1.0%."""
    assert risk.actual_risk_pct(5000, 1, 100.0, 42.50) == 1.15


def test_actual_risk_is_zero_for_a_non_position():
    assert risk.actual_risk_pct(5000, 0, 100.0, 42.50) == 0.0
    assert risk.actual_risk_pct(0, 1, 100.0, 42.50) == 0.0
    assert risk.actual_risk_pct(5000, 1, 100.0, 100.0) == 0.0


def test_the_buy_row_carries_it(temp_journal):
    tid = temp_journal.log_trade("ARM", "BUY", 1, 100.0,
                                 reason="pullback_in_uptrend",
                                 risk_pct_actual=1.15)
    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row["risk_pct_actual"] == 1.15


def test_the_column_is_optional_for_every_other_path(temp_journal):
    """Exits, CEO order sheets and the intern desk do not size — they must
    keep working without passing it."""
    tid = temp_journal.log_trade("SPCX", "SELL", 1, 150.0, pnl_usd=-4.25)
    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row["risk_pct_actual"] is None


def test_the_migration_is_guarded_and_idempotent(temp_journal):
    """init_db runs on every import; a second ALTER would raise."""
    temp_journal.init_db()
    temp_journal.init_db()
    conn = sqlite3.connect(temp_journal.DB_FILE)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
    conn.close()
    assert "risk_pct_actual" in cols


# ============================================ the worker is wired to it

def test_the_worker_sizes_with_the_configured_tolerance():
    with open("streamlit_app.py", encoding="utf-8") as f:
        src = f.read()
    assert "lot_tolerance = risk.min_lot_tolerance(config)" in src
    assert "min_lot_tolerance=lot_tolerance" in src


def test_the_worker_journals_the_actual_risk_on_the_buy():
    with open("streamlit_app.py", encoding="utf-8") as f:
        src = f.read()
    sized = src.index("risk_pct_actual = risk.actual_risk_pct(")
    buy = src.index('journal.log_trade(ticker, "BUY"', sized)
    assert "risk_pct_actual=risk_pct_actual" in src[buy:buy + 500]


def test_size_zero_journaling_survived_the_change():
    """A refusal past the tolerance must still record the arithmetic — that
    reporting is what produced the evidence for this order."""
    with open("streamlit_app.py", encoding="utf-8") as f:
        src = f.read()
    assert "stop_distance_usd=" in src and "risk_budget_usd=" in src
