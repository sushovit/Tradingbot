"""
S19 — Discord trade reporting. Formatting, width, splitting, and that an
alert failure can never reach the trading loop. No real webhook is ever
called (conftest._no_discord).
"""

import json
import sqlite3
import threading
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

import trade_alerts as ta
import trade_table as tt

ET = ZoneInfo("America/New_York")
TS = datetime(2026, 10, 9, 11, 28, tzinfo=ET)


def fields(embed):
    return dict(embed["fields"])


# ================================================== BUY

def test_buy_embed_has_every_field():
    e = ta.buy_embed("PLTR", 2, 202.815, TS, target=217.69, stop=195.75,
                     setup="mean_reversion_reclaim", conviction=74)
    f = fields(e)
    assert e["title"] == "🟢 BUY PLTR" and e["color"] == ta.GREEN
    assert f["Qty"] == "2" and f["Fill"] == "$202.815"
    assert ta._price(154.25) == "$154.25" and ta._price(5000) == "$5,000.00"
    assert f["Time"] == "2026-10-09 11:28 ET / 21:13 Nepal"
    assert f["Target"] == "$217.69 (+7.3%)"
    assert f["Stop"] == "$195.75 (-3.5%)"
    assert f["Setup"] == "mean_reversion_reclaim" and f["Conviction"] == "74"
    assert f["$ at risk"] == "$14.13"


def test_rules_only_buy_says_so():
    assert fields(ta.buy_embed("X", 1, 10.0, TS))["Conviction"] == "rules-only"


# ================================================== SELL, every exit type

@pytest.mark.parametrize("reason, fill, win", [
    ("Profit Target", 129.0, True),            # target
    ("Stop Loss", 100.0, False),               # lock at breakeven: not a win
    ("Stop Loss", 104.0, True),                # lock above entry
    ("Stop Loss", 95.0, False),                # structural stop
    ("Owner close", 106.0, True),              # owner
    ("Hard Exit Date", 98.0, False),           # time
    ("Stop Loss (synced)", 95.0, False),       # found by sync
])
def test_sell_embed_for_each_exit_type(reason, fill, win):
    entry_ts = datetime(2026, 10, 7, 9, 35, tzinfo=ET)
    e = ta.sell_embed("SPCX", 1, fill, TS, entry=100.0, reason=reason,
                      entry_ts=entry_ts, original_stop=95.0, target=115.0)
    f = fields(e)
    assert e["color"] == (ta.GREEN if win else ta.RED)
    assert e["title"].startswith("✅" if win else "🔴")
    assert reason in e["title"] and f["Exit"] == reason
    pnl = fill - 100.0
    assert f["P&L"].startswith(("+$" if pnl >= 0 else "-$") + f"{abs(pnl):,.2f}")
    assert f["R"] == f"{pnl / 5.0:+.2f}R"
    assert f["Held"] == "2d 1h"
    assert f["Toward target"] == f"{pnl / 15.0 * 100:.0f}% of the way"


def test_describe_exit_reads_the_journal(temp_journal):
    did = temp_journal.log_decision("SPCX", "mean_reversion_reclaim",
                                    {"stop": 144.445, "target": 183.065},
                                    {"approved": True})
    temp_journal.log_trade("SPCX", "BUY", 1, 154.25,
                           reason="mean_reversion_reclaim", decision_id=did)
    sell_id, _, _ = temp_journal.record_exit("SPCX", 1, 163.41, "Exit (synced)",
                                             decision_id=did, entry_price=154.25)
    f = fields(ta.describe_exit(sell_id))
    assert f["P&L"] == "+$9.16 (+5.94%)"
    assert f["R"] == "+0.93R"
    assert f["Toward target"] == "32% of the way"


# ================================================== never blocks, never raises

def test_a_failing_send_never_raises_into_the_caller():
    def boom(embed):
        raise RuntimeError("Discord is down")
    t = ta.post(ta.buy_embed("X", 1, 10.0, TS), config={}, sender=boom)
    t.join(5)                               # the failure stays in the thread
    assert not t.is_alive()


def test_post_returns_immediately_while_the_send_is_slow():
    release = threading.Event()
    t = ta.post(ta.buy_embed("X", 1, 10.0, TS), config={},
                sender=lambda e: release.wait(5))
    assert t.is_alive()                     # caller already back
    release.set()
    t.join(5)


def test_alert_helpers_swallow_their_own_errors(monkeypatch):
    monkeypatch.setattr(ta, "describe_exit",
                        lambda *a: (_ for _ in ()).throw(ValueError("bad row")))
    assert ta.alert_exit(123) is None
    assert ta.alert_buy(ticker="X") is None       # missing fields: no raise


def test_alerts_respect_the_config_switch():
    sent = []
    assert ta.post(ta.buy_embed("X", 1, 10.0, TS),
                   config={"discord": {"trade_alerts": False}},
                   sender=sent.append) is None
    assert sent == []


def test_shipped_config_has_both_switches_on():
    cfg = json.load(open("bot_config.json", encoding="utf-8"))
    assert cfg["discord"] == {"trade_alerts": True, "daily_table": True}


def test_the_worker_fires_alerts_in_the_background():
    with open("streamlit_app.py", encoding="utf-8") as f:
        src = f.read()
    assert "trade_alerts.alert_exit(trade_id, state)" in src
    assert "trade_alerts.alert_buy(" in src
    assert 'send_discord_notification(ticker, "BUY"' not in src
    assert 'send_discord_notification(ticker, "SELL"' not in src


# ================================================== the day table

def journal_with(trades):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, timestamp TEXT,"
                 " ticker TEXT, action TEXT, qty REAL, price REAL, pnl_usd REAL,"
                 " reason TEXT, decision_id INTEGER)")
    conn.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY, context TEXT)")
    for i, (tk, buy_ts, buy, sell_ts, sell, reason, qty) in enumerate(trades, 1):
        conn.execute("INSERT INTO decisions VALUES (?,?)",
                     (i, json.dumps({"stop": buy * 0.95, "target": buy * 1.15})))
        conn.execute("INSERT INTO trades (timestamp,ticker,action,qty,price,"
                     "pnl_usd,reason,decision_id) VALUES (?,?,?,?,?,?,?,?)",
                     (buy_ts, tk, "BUY", qty, buy, 0, "setup", i))
        if sell_ts:
            conn.execute("INSERT INTO trades (timestamp,ticker,action,qty,price,"
                         "pnl_usd,reason,decision_id) VALUES (?,?,?,?,?,?,?,?)",
                         (sell_ts, tk, "SELL", qty, sell, (sell - buy) * qty,
                          reason, i))
    return conn


DAY = date(2026, 10, 14)


@pytest.mark.parametrize("reason, sell, expected", [
    ("Profit Target", 115.0, "target"), ("Stop Loss", 101.0, "lock"),
    ("Stop Loss", 95.0, "stop"), ("Owner close", 99.0, "owner"),
    ("Manual Override", 99.0, "owner"), ("Exit (synced)", 104.0, "owner"),
    ("Hard Exit Date", 99.0, "time")])
def test_result_column_for_each_exit_type(reason, sell, expected):
    conn = journal_with([("AAA", "2026-10-13 10:00:00", 100.0,
                          "2026-10-14 11:00:00", sell, reason, 1)])
    lines = tt.day_table(tt.load_trades(conn), DAY)
    row = next(l for l in lines if l.startswith("AAA"))
    assert row.rstrip().endswith(expected[:5])


def test_every_line_is_at_most_50_chars_even_with_big_numbers():
    conn = journal_with([
        ("NVR", "2026-10-14 10:00:00", 7654.32, "2026-10-14 15:00:00", 7012.5,
         "Stop Loss", 1),
        ("PENNY", "2026-10-14 10:00:00", 1.2345, "2026-10-14 15:00:00", 1.31,
         "Profit Target", 999),
        ("BRKAX", "2026-10-14 10:00:00", 512.01, None, None, None, 3)])
    open_pos = {"BRKAX": {"qty": 3, "entry": 512.01, "target": 600.0,
                          "stop": 490.0, "unrealized": -1234.56}}
    lines = tt.day_table(tt.load_trades(conn), DAY, open_pos, 123456.78)
    lines += tt.weekly_lines(tt.load_trades(conn), DAY, 123456.78)
    assert max(len(l) for l in lines) <= 50
    assert any("open" in l and l.startswith("BRKAX") for l in lines)


def test_summary_counts_wins_losses_targets_and_locks():
    conn = journal_with([
        ("A", "2026-10-14 09:40:00", 100.0, "2026-10-14 15:00:00", 115.0,
         "Profit Target", 1),
        ("B", "2026-10-13 09:40:00", 100.0, "2026-10-14 12:00:00", 101.0,
         "Stop Loss", 1),
        ("C", "2026-10-14 10:00:00", 100.0, "2026-10-14 13:00:00", 95.0,
         "Stop Loss", 1)])
    text = "\n".join(tt.day_table(tt.load_trades(conn), DAY, {}, 5011.0))
    assert "Trades 3" in text and "W 2" in text and "L 1" in text
    assert "Win 67%" in text and "Realized +$11.00" in text
    assert "Targets 1" in text and "Locks 1" in text
    assert "Equity $5,011.00" in text


def test_long_tables_split_under_2000_chars_in_code_blocks():
    lines = [f"ROW{i:04d} " + "x" * 40 for i in range(200)]
    msgs = tt.to_messages("Day trades — test", lines)
    assert len(msgs) > 1
    for i, m in enumerate(msgs, 1):
        assert len(m) <= 2000
        assert m.count("```") == 2
        assert f"({i}/{len(msgs)})" in m
    body = [l for m in msgs for l in m.split("\n") if l.startswith("ROW")]
    assert body == lines                     # nothing lost, nothing reordered


def test_a_short_table_is_one_message():
    assert len(tt.to_messages("t", ["a", "b"])) == 1


def test_weekly_runs_on_friday_and_counts_from_the_account_switch():
    conn = journal_with([
        ("OLD", "2026-10-08 10:00:00", 100.0, "2026-10-09 11:28:00", 105.0,
         "Exit (synced)", 1),                  # old account, closed pre-switch
        ("NEW", "2026-10-13 10:00:00", 100.0, "2026-10-16 11:00:00", 115.0,
         "Profit Target", 1)])
    friday = date(2026, 10, 16)
    msgs = tt.build_messages(conn, friday, broker=None)
    assert len(msgs) == 2 and "Week — 2026-10-16" in msgs[1]
    since = msgs[1].split("Since")[1]
    assert "Trades 1" in since and "Realized +$15.00" in since
    assert len(tt.build_messages(conn, date(2026, 10, 15), broker=None)) == 1


def test_drop_never_fails_because_the_table_did(monkeypatch, capsys):
    import drop
    monkeypatch.setattr(tt, "build_messages",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("db")))
    assert drop.post_day_trades(day=DAY, broker=object()) == []
    assert "day trades table failed" in capsys.readouterr().out
