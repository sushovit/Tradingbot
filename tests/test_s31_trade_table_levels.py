"""S31 (CEO 2026-10-17) - open positions show the bracket's LIVE target and
stop (the S28 cap, the ladder's raises); a closed trade keeps the levels it
held at exit, not the gatekeeper's uncapped proposal."""

import json
import sqlite3
from datetime import date
from types import SimpleNamespace

import trade_table

REGN_STATE = {"in_position": True, "entry_price": 802.61, "shares_held": 1,
              "trailing_stop_price": 756.25, "profit_target_price": 862.58,
              "uncapped_target": 956.21, "target_capped": True}


def regn_open(temp_journal):
    did = temp_journal.log_decision(
        "REGN", "mean_reversion_reclaim",
        {"entry": 802.61, "target": 956.21, "stop": 756.25},
        {"approved": True, "conviction_score": 80})
    temp_journal.log_trade("REGN", "BUY", 1, 802.61,
                           reason="mean_reversion_reclaim", decision_id=did)
    return did


class FakeBroker:
    def get_positions(self):
        return [SimpleNamespace(symbol="REGN", qty="1", avg_entry_price="802.61",
                                unrealized_pl="-11.87")]

    def get_equity(self):
        return 5000.0


def live(tmp_path):
    pf = tmp_path / "positions.json"
    pf.write_text(json.dumps({"REGN": REGN_STATE}))
    return trade_table.live_open_positions(FakeBroker(), str(pf))


def test_regn_open_shows_the_live_capped_target_and_stop(temp_journal, tmp_path):
    regn_open(temp_journal)
    conn = sqlite3.connect(temp_journal.DB_FILE)
    rows = trade_table.account_trades(conn, since="2026-01-01",
                                      open_positions=live(tmp_path))
    r = [x for x in rows if x["Ticker"] == "REGN"][0]
    assert (r["Target"], r["Stop"], r["Result"]) == (862.58, 756.25, "open")


def test_regn_in_the_discord_day_table(temp_journal, tmp_path):
    regn_open(temp_journal)
    conn = sqlite3.connect(temp_journal.DB_FILE)
    lines = trade_table.day_table(trade_table.load_trades(conn),
                                  date(2026, 10, 16), live(tmp_path))
    regn = [ln for ln in lines if ln.startswith("REGN")][0]
    # the phone-width table prints 4 significant figures: 862.6 / 756.2
    assert "862.6" in regn and "756.2" in regn and "956" not in regn


def test_a_closed_trade_keeps_the_levels_it_held_at_exit(temp_journal):
    did = regn_open(temp_journal)
    state = dict(REGN_STATE, trailing_stop_price=802.61)   # ladder raised it
    temp_journal.record_exit("REGN", 1, 862.58, "Profit Target", decision_id=did,
                             broker_order_id="t1", entry_price=802.61,
                             **temp_journal.exit_levels(state))
    conn = sqlite3.connect(temp_journal.DB_FILE)
    t = trade_table.load_trades(conn)[0]
    assert (t["target"], t["stop"]) == (862.58, 802.61)
    r = trade_table.account_trades(conn, since="2026-01-01")[0]
    assert (r["Target"], r["Stop"], r["Result"]) == (862.58, 802.61, "target")


def test_an_old_sell_without_levels_falls_back_to_the_proposal(temp_journal):
    did = regn_open(temp_journal)
    temp_journal.log_trade("REGN", "SELL", 1, 760.0, reason="Stop Loss",
                           decision_id=did)
    conn = sqlite3.connect(temp_journal.DB_FILE)
    t = trade_table.load_trades(conn)[0]
    assert (t["target"], t["stop"]) == (956.21, 756.25)


def test_the_exit_path_journals_the_levels(temp_journal, monkeypatch):
    import streamlit_app
    monkeypatch.setattr(streamlit_app, "journal", temp_journal)
    monkeypatch.setattr(streamlit_app, "write_positions", lambda p: None)
    monkeypatch.setattr(streamlit_app, "append_trade_log", lambda *a, **k: None)
    monkeypatch.setattr(streamlit_app.trade_alerts, "alert_exit", lambda *a, **k: None)
    did = regn_open(temp_journal)
    positions = {"REGN": dict(REGN_STATE, decision_id=did)}
    from datetime import datetime
    streamlit_app.handle_position_exit(None, positions, "REGN", positions["REGN"],
                                       757.0, "Stop Loss", datetime(2026, 10, 19, 10),
                                       broker_order_id="s1")
    conn = sqlite3.connect(temp_journal.DB_FILE)
    row = conn.execute("SELECT exit_target, exit_stop FROM trades "
                       "WHERE action='SELL'").fetchone()
    assert row == (862.58, 756.25)
