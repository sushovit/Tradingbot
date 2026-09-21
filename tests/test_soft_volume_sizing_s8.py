"""
S8 (agenda 10.8): the soft volume band for reclaims, and conviction-scaled
sizing. No network.

(a) SOFT VOLUME. The reclaim detector refused anything at or below 1.3x the
20-bar average outright. A deterministic filter is the right tool for a
condition that is always fatal; volume shortfall is not one, it is a
judgement. Between 1.0x and the configured multiplier the signal now fires
with `soft_volume` set, the prompt STATES the shortfall with its number, and
the gatekeeper makes the call. Below 1.0x the reclaim was not participated
in at all and is still refused in code.

The band and the v5 rubric agree by construction: the rubric requires volume
at or above the 20-bar average, and the soft floor IS that average.

(b) CONVICTION SIZING. Conviction >= 80 risks 1.25x the normal budget. The
multiplier scales the BUDGET only. position_size applies max_position_pct
and the no-margin rule afterwards, so it can never buy a larger position
than the cap allows - which is the property most likely to be broken by a
later edit, so it is tested from both directions.
"""

import json
import sqlite3

import pandas as pd
import pytest

import risk
import streamlit_app as app
from strategies.mean_reversion_reclaim import MeanReversionReclaim


CONFIG = json.load(open("bot_config.json", encoding="utf-8"))
RECLAIM_CONFIG = {"volume_multipliers": {"mean_reversion_reclaim": 1.3}}
PROFILE = {"fast_ema": 9, "slow_ema": 21, "adx_threshold": 30,
           "risk_per_trade_pct": 1.0, "rr_ratio": 3.0, "atr_multiplier": 2.0,
           "trailing_stop_type": "ATR", "trailing_stop_value": 2.5,
           "use_volume_filter": True}


def reclaim_df(vol_ratio):
    """A washout-and-reclaim whose reclaim bar trades `vol_ratio` x the
    20-bar average.

    Four bars at 100, a ten-bar slide to 80, then six flat bars so EMA9
    catches down to the base — a reclaim bar has to close ABOVE EMA9, and a
    straight-line decline leaves the average stranded above price no matter
    how big the reclaim. Then the reclaim bar, then an entry bar opening
    above the reclaim midpoint.
    """
    rows = [(100, 101, 99, 100, 100_000)] * 4
    for i in range(10):
        c = 98 - i * 2
        rows.append((c + 1.0, c + 1.2, c - 0.8, c, 100_000))
    rows += [(79, 79.6, 78.4, 79, 100_000)] * 6
    rows.append((79.2, 83.4, 78.6, 83.0, int(100_000 * vol_ratio)))
    rows.append((83.2, 84.0, 82.9, 83.6, 120_000))
    idx = pd.date_range("2026-08-20", periods=len(rows), freq="D")
    return pd.DataFrame(rows, index=idx,
                        columns=["open", "high", "low", "close", "volume"])


def detect(vol_ratio, config=None):
    return MeanReversionReclaim().detect(
        reclaim_df(vol_ratio),
        {"ticker": "SWKS", "risk_profile": PROFILE,
         "config": config if config is not None else RECLAIM_CONFIG})


# ============================================ (a) the band

def test_a_full_volume_reclaim_is_still_an_ordinary_signal():
    result = detect(1.5)
    assert result.__class__.__name__ == "Signal"
    assert result.extras["soft_volume"] is False
    assert result.extras["volume_ratio"] == pytest.approx(1.5, abs=0.01)


@pytest.mark.parametrize("ratio", [1.0, 1.05, 1.2, 1.29])
def test_the_soft_band_now_produces_a_signal_not_a_rejection(ratio):
    """The change itself: every one of these was a `volume_low` Rejection."""
    result = detect(ratio)
    assert result.__class__.__name__ == "Signal", ratio
    assert result.extras["soft_volume"] is True
    assert result.extras["volume_ratio"] == pytest.approx(ratio, abs=0.01)
    assert result.extras["volume_mult_required"] == 1.3


def test_below_the_floor_is_still_refused_in_code():
    """1.0x is a floor, not a formality. A reclaim nobody participated in
    does not reach the gatekeeper at all."""
    result = detect(0.99)
    assert result.__class__.__name__ == "Rejection"
    assert result.filter_name == "volume_low"
    assert "below" in result.details and "floor" in result.details


def test_the_upper_edge_of_the_band_is_a_full_signal():
    """[1.0, 1.3): at exactly the multiplier the signal is not soft."""
    result = detect(1.30)
    assert result.__class__.__name__ == "Signal"
    assert result.extras["soft_volume"] is False


def test_the_band_tracks_the_configured_multiplier():
    """Not a hardcoded 1.3 — if the multiplier moves, the band moves."""
    loose = {"volume_multipliers": {"mean_reversion_reclaim": 1.1}}
    assert detect(1.2, loose).extras["soft_volume"] is False
    assert detect(1.05, loose).extras["soft_volume"] is True


def test_a_multiplier_below_the_floor_is_not_tightened_by_it():
    """A config asking for 0.8x must not be silently raised to 1.0x."""
    loose = {"volume_multipliers": {"mean_reversion_reclaim": 0.8}}
    result = detect(0.9, loose)
    assert result.__class__.__name__ == "Signal"
    assert result.extras["soft_volume"] is False


def test_the_reasoning_says_it_is_soft():
    assert "SOFT" in detect(1.1).reasoning
    assert "SOFT" not in detect(1.5).reasoning


def test_the_live_config_puts_the_band_at_one_to_one_point_three():
    assert CONFIG["volume_multipliers"]["mean_reversion_reclaim"] == 1.3


# ============================================ (a) the prompt states it

def test_the_setup_description_states_the_shortfall_with_its_number():
    signal = detect(1.12)
    described = app.describe_setup(signal)
    assert "SOFT VOLUME" in described
    assert "1.12x the 20-bar average" in described
    assert "1.30x this setup normally requires" in described


def test_the_description_says_it_is_deliberate():
    """Without this the model reads a flagged shortfall as a defect report
    and declines on it — the opposite of handing it the judgement."""
    described = app.describe_setup(detect(1.12))
    assert "deliberately, not by oversight" in described
    assert "yours to" in described


def test_the_description_reconciles_the_band_with_the_v5_rubric():
    """The rubric requires volume >= the 20-bar average. A soft signal meets
    it, and the prompt must say so or the model will reject on the rubric."""
    assert "rubric" in app.describe_setup(detect(1.12))


def test_a_full_volume_signal_gets_no_note_at_all():
    signal = detect(1.5)
    assert app.describe_setup(signal) == signal.reasoning
    assert "SOFT VOLUME" not in app.describe_setup(signal)


def test_the_note_survives_missing_numbers():
    """extras written by an older signal must not crash the prompt build."""
    signal = detect(1.12)
    signal.extras["volume_ratio"] = None
    described = app.describe_setup(signal)
    assert "SOFT VOLUME" in described
    assert "below the volume this setup normally requires" in described


def test_the_kwargs_carry_the_described_setup():
    with open("streamlit_app.py", encoding="utf-8") as f:
        src = f.read()
    assert '"setup_description": describe_setup(signal),' in src
    assert '"setup_description": signal.reasoning,' not in src


def test_the_flag_is_journaled_on_the_decision():
    with open("streamlit_app.py", encoding="utf-8") as f:
        src = f.read()
    idx = src.index("decision_context = {")
    block = src[idx:idx + 900]
    assert '"soft_volume": bool(signal.extras.get("soft_volume")),' in block
    assert '"volume_ratio": signal.extras.get("volume_ratio"),' in block


# ============================================ (b) conviction sizing

def test_the_ratified_multiplier():
    assert CONFIG["conviction_size_mult"] == {"80": 1.25}


@pytest.mark.parametrize("score,expected", [(0, 1.0), (70, 1.0), (79, 1.0),
                                            (79.9, 1.0), (80, 1.25),
                                            (95, 1.25), (100, 1.25)])
def test_the_threshold_is_at_eighty(score, expected):
    assert risk.conviction_multiplier(score, CONFIG) == expected


def test_an_unscored_signal_is_never_scaled_up():
    """A missing conviction is not a high one. With use_claude_filter off
    there is no score at all, and that must size normally."""
    assert risk.conviction_multiplier(None, CONFIG) == 1.0
    assert risk.conviction_multiplier("high", CONFIG) == 1.0


def test_no_config_means_no_scaling():
    assert risk.conviction_multiplier(95, {}) == 1.0
    assert risk.conviction_multiplier(95, None) == 1.0


def test_the_highest_cleared_tier_wins():
    """The map can grow tiers without code changes."""
    tiered = {"conviction_size_mult": {"80": 1.25, "90": 1.5, "70": 1.1}}
    assert risk.conviction_multiplier(75, tiered) == 1.1
    assert risk.conviction_multiplier(85, tiered) == 1.25
    assert risk.conviction_multiplier(95, tiered) == 1.5
    assert risk.conviction_multiplier(65, tiered) == 1.0


def test_junk_entries_are_skipped_not_fatal():
    junk = {"conviction_size_mult": {"eighty": 1.25, "80": "wide", "75": -2}}
    assert risk.conviction_multiplier(95, junk) == 1.0


def test_the_multiplier_actually_buys_more_shares():
    """$5,000 x 1% = $50 against a $10 stop distance is 5 shares; at 1.25x
    the budget is $62.50, which is 6."""
    assert risk.position_size(5000, 1.0, 100.0, 90.0,
                              position_cap_pct=0.25) == 5
    assert risk.position_size(5000, 1.0 * 1.25, 100.0, 90.0,
                              position_cap_pct=0.25) == 6


# ============================================ (b) the cap still binds

def test_the_multiplier_can_never_push_notional_over_the_cap():
    """The property most likely to be broken by a later edit. A $50 stock
    with a $1 stop: the scaled budget wants 62 shares ($3,100), the 25% cap
    allows $1,250, so 25 shares is what is bought."""
    scaled = risk.position_size(5000, 1.25, 50.0, 49.0, position_cap_pct=0.25)
    assert scaled == 25
    assert scaled * 50.0 <= 5000 * 0.25


@pytest.mark.parametrize("mult", [1.0, 1.25, 1.5, 3.0])
def test_no_multiplier_breaches_the_cap(mult):
    for entry, stop in ((50.0, 49.0), (100.0, 98.0), (270.11, 254.84)):
        qty = risk.position_size(5000, 1.0 * mult, entry, stop,
                                 position_cap_pct=0.25)
        assert qty * entry <= 5000 * 0.25 + 1e-9, (mult, entry)


def test_the_scaled_size_still_has_to_clear_check_signal():
    """Sizing is not approval: the notional gate runs again afterwards."""
    qty = risk.position_size(5000, 1.25, 50.0, 49.0, position_cap_pct=0.25)
    ok, reason = risk.check_signal(50.0, 49.0, 55.0, 5000,
                                   notional_usd=qty * 50.0, max_positions=4,
                                   position_cap_pct=0.25)
    assert ok is True, reason


def test_the_no_margin_rule_still_binds_under_the_multiplier():
    qty = risk.position_size(5000, 1.25, 50.0, 49.0,
                             open_notional_usd=4900.0, position_cap_pct=0.25)
    assert qty * 50.0 <= 100.0


# ============================================ (b) journaled on the BUY row

def test_the_buy_row_carries_the_multiplier(temp_journal):
    tid = temp_journal.log_trade("SWKS", "BUY", 6, 100.0,
                                 reason="mean_reversion_reclaim",
                                 risk_pct_actual=1.25, size_mult=1.25)
    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row["size_mult"] == 1.25
    assert row["risk_pct_actual"] == 1.25


def test_the_column_is_optional(temp_journal):
    tid = temp_journal.log_trade("SPCX", "SELL", 1, 150.0)
    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
    conn.close()
    assert row["size_mult"] is None


def test_the_migration_is_guarded(temp_journal):
    temp_journal.init_db()
    temp_journal.init_db()
    conn = sqlite3.connect(temp_journal.DB_FILE)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
    conn.close()
    assert {"size_mult", "risk_pct_actual"} <= cols


def test_the_worker_sizes_and_journals_with_the_multiplier():
    with open("streamlit_app.py", encoding="utf-8") as f:
        src = f.read()
    mult = src.index("size_mult = risk.conviction_multiplier(conviction, config)")
    assert "entry_risk_pct = entry_risk_pct * size_mult" in src[mult:mult + 200]
    buy = src.index('journal.log_trade(ticker, "BUY"', mult)
    assert "size_mult=size_mult" in src[buy:buy + 500]


def test_conviction_defaults_to_none_before_the_gatekeeper_block():
    """With use_claude_filter off, `conviction` would otherwise be unbound at
    the sizing site and crash the loop."""
    with open("streamlit_app.py", encoding="utf-8") as f:
        src = f.read()
    default = src.index("conviction = None")
    gate = src.index("if use_claude_filter:", default)
    sized = src.index("size_mult = risk.conviction_multiplier", gate)
    assert default < gate < sized
