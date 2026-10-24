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

pytestmark = pytest.mark.real_post_async   # tests post_async itself

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
    ("Stop Loss", 100.0, None),                # lock at breakeven: grey
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
    color, icon = {True: (ta.GREEN, "✅"), False: (ta.RED, "🔴"),
                   None: (ta.GREY, "⚪")}[win]
    assert e["color"] == color and e["title"].startswith(icon)
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


def test_a_ceo_close_alerts_without_waiting(temp_journal, monkeypatch):
    """S19b: CEO closes alert again - via post_async, never waited on
    (the hung-Discord timing test lives in test_s19b_async_discord)."""
    import orders
    calls = []
    monkeypatch.setattr(ta, "alert_exit",
                        lambda *a, **k: calls.append((a, k)))
    did = temp_journal.log_decision("PLTR", "ceo", {"stop": 195.0}, {})
    temp_journal.log_trade("PLTR", "BUY", 2, 202.8, reason="CEO x",
                           decision_id=did)

    class Broker:
        def close_position(self, t):
            return type("O", (), {"id": "close-1"})()

        def get_latest_price(self, t):
            return 205.3

    positions = {"PLTR": {"in_position": True, "entry_price": 202.8,
                          "shares_held": 2, "decision_id": did}}
    orders.execute_order({"action": "CLOSE", "ticker": "PLTR"}, Broker(),
                         did, positions)
    assert len(calls) == 1 and calls[0][1] == {}      # no wait= kwarg
    orders.execute_order({"action": "CLOSE", "ticker": "PLTR"}, Broker(),
                         did, positions)               # already journaled
    assert len(calls) == 1


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
    """S19b: the message is built on the background thread, so a bad row
    fails THERE - the caller gets its thread back and nothing raises."""
    monkeypatch.setattr(ta, "describe_exit",
                        lambda *a: (_ for _ in ()).throw(ValueError("bad row")))
    sent = []
    monkeypatch.setattr(ta, "_send", lambda embed: sent.append(embed))
    ta.alert_exit(123).join(2)
    ta.alert_buy(ticker="X").join(2)          # missing fields: no raise
    assert sent == [] and ta.alert_exit(None) is None


def test_alerts_respect_the_config_switch():
    sent = []
    t = ta.post(ta.buy_embed("X", 1, 10.0, TS),
                config={"discord": {"trade_alerts": False}},
                sender=sent.append)
    t.join(2)
    assert sent == []


def test_shipped_config_has_every_switch_on():
    cfg = json.load(open("bot_config.json", encoding="utf-8"))
    assert cfg["discord"] == {"trade_alerts": True, "daily_table": True,
                              "missed_movers": True,      # S33
                              "catalysts": True,          # S36
                              "scoreboard": True,         # CEO 10-21
                              "watching": True,           # CEO 10-23
                              "credit_report": True}      # CEO 10-24


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


# ================================================== partial sells

def with_partial(conn, close=True):
    """AAA: BUY 4 @ 100 (decision 1), TAKE_PARTIAL 2 @ 110 on 10-13,
    close 2 @ 104 on 10-14 (unless close=False)."""
    rows = [("2026-10-13 12:00:00", "SELL", 2, 110.0, 20.0, "CEO TAKE_PARTIAL")]
    if close:
        rows.append(("2026-10-14 11:00:00", "SELL", 2, 104.0, 8.0,
                     "Stop Loss"))
    for ts, act, q, px, pnl, why in rows:
        conn.execute("INSERT INTO trades (timestamp,ticker,action,qty,price,"
                     "pnl_usd,reason,decision_id) VALUES (?,?,?,?,?,?,?,1)",
                     (ts, "AAA", act, q, px, pnl, why))
    return conn


def test_a_partial_and_its_close_are_one_trade():
    conn = with_partial(journal_with([("AAA", "2026-10-13 10:00:00", 100.0,
                                       None, None, None, 4)]))
    (t,) = tt.load_trades(conn)
    assert t["qty"] == 4 and t["sell"] == 107.0 and t["pnl"] == 28.0
    assert t["sell_ts"].startswith("2026-10-14") and t["reason"] == "Stop Loss"
    lines = tt.day_table([t], DAY)
    assert sum(l.startswith("AAA") for l in lines) == 1
    assert "Realized +$28.00" in "\n".join(lines)


def test_a_partly_sold_trade_shows_the_sold_part_and_the_rest_open():
    conn = with_partial(journal_with([("AAA", "2026-10-13 10:00:00", 100.0,
                                       None, None, None, 4)]), close=False)
    (t,) = tt.load_trades(conn)
    assert t["qty"] == 2 and t["sell"] == 110.0 and t["pnl"] == 20.0
    open_pos = {"AAA": {"qty": 2, "entry": 100.0, "unrealized": 6.0}}
    lines = tt.day_table([t], date(2026, 10, 13), open_pos)
    rows = [l for l in lines if l.startswith("AAA")]
    assert len(rows) == 2
    assert rows[0].rstrip().endswith("part") and rows[1].endswith("open")
    assert "Realized +$20.00" in "\n".join(lines)


def test_a_later_trade_in_the_same_ticker_keeps_its_own_sell():
    conn = journal_with([
        ("AAA", "2026-10-13 10:00:00", 100.0, "2026-10-13 11:00:00", 95.0,
         "Stop Loss", 1),
        ("AAA", "2026-10-14 10:00:00", 96.0, "2026-10-14 11:00:00", 110.4,
         "Profit Target", 1)])
    a, b = tt.load_trades(conn)
    assert (a["sell"], a["reason"]) == (95.0, "Stop Loss")
    assert (b["sell"], b["reason"]) == (110.4, "Profit Target")


# ================================================== desk up

def test_startup_embed_all_clear_is_green():
    e = ta.startup_embed(TS, 61090, equity=5000.0, positions=[],
                         universe_n=162,
                         checks=[("Broker", True, "Alpaca PAPER connected"),
                                 ("Gatekeeper (Anthropic)", True,
                                  "credits available")])
    f = fields(e)
    assert e["title"] == "🟢 Desk up" and e["color"] == ta.GREEN
    assert f["Time"] == "2026-10-09 11:28 ET / 21:13 Nepal"
    assert f["PID"] == "61090" and f["Capital"] == "$5,000.00"
    assert f["Open positions"] == "none" and f["Universe"] == "162 names"
    assert f["Gatekeeper (Anthropic)"] == "✅ credits available"


def test_a_failed_preflight_turns_the_up_message_amber():
    e = ta.startup_embed(TS, 1, positions=["PLTR", "SPCX"],
                         checks=[("Broker", True, "ok"),
                                 ("Gatekeeper (Anthropic)", False,
                                  "billing: no credits")])
    f = fields(e)
    assert e["color"] == ta.AMBER and "1 pre-flight problem" in e["title"]
    assert f["Gatekeeper (Anthropic)"] == "⚠️ billing: no credits"
    assert f["Open positions"] == "PLTR, SPCX" and "Capital" not in f


def test_startup_alert_has_its_own_switch_and_never_raises(monkeypatch):
    sent = []
    monkeypatch.setattr(ta, "_send", lambda embed: sent.append(embed))
    ta.post(ta.startup_embed(TS, 1), config={"discord": {"startup": False}},
            key="startup").join(2)
    assert sent == []
    ta.post(ta.startup_embed(TS, 1),
            config={"discord": {"trade_alerts": False}}, key="startup").join(2)
    assert len(sent) == 1                  # fills off does not silence UP
    ta.alert_startup(ts="not a datetime", pid=1).join(2)   # fails off-thread
    assert len(sent) == 1


def test_desk_up_is_sent_from_inside_the_loop_only():
    """S19b: "Desk up" comes from the loop after the first completed cycle -
    never from the start-up path before it (CEO audit, 2026-10-14)."""
    with open("streamlit_app.py", encoding="utf-8") as f:
        src = f.read()
    start = src.index("def _worker_loop():")
    loop = src.index("    while os.path.exists(LOCK_FILE):\n        cycle_count", start)
    assert "alert_startup" not in src[start:loop]
    assert "alert_startup(" in src[loop:src.index("\ndef ", loop)]


# ================================================== dashboard: $5,000 account table

def test_dashboard_table_is_the_5k_account_from_the_journal():
    conn = journal_with([
        # retired account, before the 12:47 takeover: excluded
        ("SWKS", "2026-09-11 09:40:00", 89.86, "2026-10-09 09:41:00", 74.63,
         "Stop Loss", 1),
        ("PLTR", "2026-10-09 09:32:00", 202.815, "2026-10-09 11:28:00",
         205.26, "Exit (synced)", 2),
        # $5,000 account
        ("AMD", "2026-10-13 09:31:00", 100.0, "2026-10-14 10:00:00", 115.0,
         "Profit Target", 3),                        # target
        ("ON", "2026-10-13 09:33:00", 80.0, "2026-10-13 15:00:00", 78.0,
         "Stop Loss (synced)", 2),                   # found by sync
        ("MU", "2026-10-14 09:32:00", 50.0, None, None, None, 10),  # open
        ("XOM", "2026-10-14 09:40:00", 100.0, "2026-10-14 11:00:00", 101.0,
         "CEO CLOSE", 1),                            # CEO exit
    ])
    conn.execute("INSERT INTO trades (timestamp,ticker,action,qty,price,pnl_usd,"
                 "reason,decision_id) VALUES ('2026-10-14 09:45:00','F','BUY',"
                 "5,10.0,0,'INTERN momentum',99)")
    rows = tt.account_trades(conn, open_positions={"MU": {"unrealized": 12.5}})
    assert [r["Ticker"] for r in rows] == ["XOM", "MU", "ON", "AMD"]
    by = {r["Ticker"]: r for r in rows}
    assert by["AMD"]["Result"] == "target" and by["AMD"]["P&L $"] == 45.0  # 3 sh
    assert by["AMD"]["P&L %"] == 15.0 and by["AMD"]["Closed (ET)"] == "2026-10-14 10:00"
    assert by["ON"]["Result"] == "stop" and by["ON"]["P&L $"] == -4.0
    assert by["MU"]["Result"] == "open" and by["MU"]["Sell"] is None
    assert by["MU"]["P&L $"] == 12.5 and by["MU"]["P&L %"] == 2.5
    assert by["XOM"]["Result"] == "exit"
    assert by["AMD"]["Setup"] == "setup" and by["AMD"]["Target"] == pytest.approx(115.0)
    s = tt.account_summary(rows, equity=5011.0)
    assert (s["trades"], s["open"], s["wins"], s["losses"]) == (4, 1, 2, 1)
    assert s["realized"] == 42.0 and s["change"] == 11.0


def test_dashboard_table_marks_a_partly_sold_trade():
    conn = with_partial(journal_with([("AAA", "2026-10-13 10:00:00", 100.0,
                                       None, None, None, 4)]), close=False)
    (r,) = tt.account_trades(conn, open_positions={"AAA": {"unrealized": 6.0}})
    assert r["Result"] == "part" and r["Sell"] is None and r["P&L $"] == 6.0


def test_dashboard_no_longer_reads_live_trades_csv():
    with open("streamlit_app.py", encoding="utf-8") as f:
        src = f.read()
    assert "pd.read_csv(TRADE_LOG_FILE)" not in src
    assert "trade_table.account_trades(" in src
