"""CEO 2026-10-22: max 1 open position per sector, checked before entry; a
blocked signal is journaled as sector_cap with its would-be levels."""

import json
import sqlite3

import risk

CFG = {"sector_cap": {"enabled": True, "max_per_sector": 1}}
SECTOR = {"KMI": "energy", "SLB": "energy", "XOM": "energy", "NVDA": "technology",
          "ZZZ": "unclassified", "YYY": "unclassified"}.get


def test_second_name_in_a_held_sector_is_blocked():
    held = {"KMI": {"in_position": True}}
    assert risk.sector_cap_block("SLB", held, CFG, SECTOR) == \
        "energy already held: KMI (max 1 per sector)"
    assert risk.sector_cap_block("NVDA", held, CFG, SECTOR) is None     # other sector


def test_closed_positions_and_the_ticker_itself_do_not_count():
    assert risk.sector_cap_block("SLB", {"KMI": {"in_position": False}}, CFG, SECTOR) is None
    assert risk.sector_cap_block("KMI", {"KMI": {"in_position": True}}, CFG, SECTOR) is None


def test_unclassified_names_are_not_capped_against_each_other():
    assert risk.sector_cap_block("ZZZ", {"YYY": {"in_position": True}}, CFG, SECTOR) is None


def test_limit_and_switch_come_from_config():
    held = {"KMI": {"in_position": True}}
    assert risk.sector_cap_block("SLB", held, {"sector_cap": {"max_per_sector": 2}}, SECTOR) is None
    assert risk.sector_cap_block("SLB", held, {"sector_cap": {"enabled": False}}, SECTOR) is None
    shipped = json.load(open("bot_config.json", encoding="utf-8"))
    assert shipped["sector_cap"] == {"enabled": True, "max_per_sector": 1}


def test_the_recorded_sector_on_a_position_wins():
    held = {"OLD": {"in_position": True, "sector": "energy"}}
    assert risk.sector_cap_block("SLB", held, CFG, SECTOR).startswith("energy already held: OLD")


def test_worker_takes_one_and_journals_the_second_as_sector_cap(temp_journal, tmp_path,
                                                               monkeypatch):
    """NVDA and AMD (both technology) signal in the same cycle: the first is
    bought, the second is journaled sector_cap WITH its would-be levels and
    never reaches the gatekeeper."""
    import streamlit_app as app
    from tests.test_never_average_down import drive
    asked, submitted = drive(app, monkeypatch, tmp_path)
    bought = [a[0] for a in submitted if a]
    rows = {t: json.loads(c) for t, v, c in sqlite3.connect(temp_journal.DB_FILE).execute(
        "SELECT ticker, verdict, context FROM decisions WHERE source='rules' "
        "AND verdict LIKE '%sector_cap%'")}
    assert len(bought) == 1 and set(rows) == {"NVDA", "AMD"} - set(bought)
    blocked = next(iter(rows))
    assert blocked not in asked                               # before the gatekeeper
    assert rows[blocked]["entry"] > rows[blocked]["stop"]     # would-be levels
    assert "technology already held" in rows[blocked]["details"]


def test_the_stale_pullback_timeframe_label_is_gone():
    cfg = json.load(open("bot_config.json", encoding="utf-8"))
    assert "pullback_in_uptrend" not in json.dumps(cfg)
