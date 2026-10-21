"""The Open Positions table speaks the LIVE rule (R/ATR ladder): the next
stop move in plain English, and a progress bar that never goes negative."""

from types import SimpleNamespace as N

import dashboard_data as d

PROFILE = {"daily_trailing": "r_atr"}        # code defaults: S29 steps


def test_before_plus_1r_the_next_move_is_breakeven():
    txt = d.next_trigger(802.61, 756.25, 756.25, 808.37, PROFILE)
    assert txt == "+1R at a 5-min close ≥ $848.97 (from 10:00 ET) → stop $802.61 (breakeven)"


def test_after_plus_1r_the_redundant_1_5r_step_is_skipped():
    # stop already at entry: +1.5R asks for entry too, so the next REAL move is +2R
    txt = d.next_trigger(100.0, 98.0, 100.0, 102.4, PROFILE)
    assert txt.startswith("+2R at a 5-min close ≥ $104.00") and "stop $102.00 (entry +1R)" in txt


def test_past_the_last_step_it_trails():
    assert d.next_trigger(100.0, 98.0, 102.0, 104.5, PROFILE).startswith(
        "Trailing: highest daily close − 2.5×ATR14")


def test_no_placeholder_text_for_the_live_rule():
    assert "Rule: r_atr" not in d.next_trigger(100.0, 98.0, 98.0, 100.0, PROFILE)


def test_to_target_is_a_0_to_100_progress():
    def row(last):
        p = N(symbol="REGN", qty="1", avg_entry_price="802.61", current_price=str(last))
        local = {"REGN": {"profit_target_price": 862.58, "trailing_stop_price": 756.25,
                          "original_stop": 756.25, "timeframe": "daily", "source": "bot"}}
        cfg = {"risk_profiles": {"Moderate": PROFILE}}
        return d.position_rows([p], local, [], cfg)[0]
    assert row(802.17)["To target %"] == 0.0                     # below entry
    assert round(row(832.595)["To target %"]) == 50
    assert row(870.0)["To target %"] == 100.0
    assert row(802.17)["Next stop move"].startswith("+1R at a 5-min close ≥ $848.97")
