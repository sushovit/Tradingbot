"""CEO 2026-10-21 - weekly scoreboard: live R/ATR vs the owner's 0.5% ladder
on every daily trade since 10-09, per trade and total, and the switch rule
(owner > live in total over >= 10 closed trades). No network."""

import json
import plistlib
import sqlite3
from datetime import date

import pandas as pd

import scoreboard as sb
import trade_table


def bars(prices, start="2026-10-14 13:35"):
    idx = pd.date_range(start, periods=len(prices), freq="5min", tz="UTC")
    return pd.DataFrame([{"open": o, "high": h, "low": l, "close": c}
                         for o, h, l, c in prices], index=idx)


def trade(ticker, buy, qty, sell=None, pnl=None, stop=95.0, target=115.0,
          buy_ts="2026-10-14 09:31:00"):
    return {"ticker": ticker, "buy": buy, "qty": qty, "sell": sell, "pnl": pnl,
            "reason": "Stop Loss" if sell else None, "buy_ts": buy_ts,
            "sell_ts": "2026-10-15 10:00" if sell else None,
            "orig_stop": stop, "target": target, "setup": "mean_reversion_reclaim"}


# 100 -> 101 (+1%: owner BE then lock) -> back down through the locks
RUNUP_THEN_FALL = [(100, 100, 100, 100), (100, 101.0, 100, 101.0),
                   (101, 101, 99.0, 99.2), (99, 99, 94, 94.5)]


def test_owner_ladder_locks_what_live_gave_back():
    t = [trade("TXN", 100.0, 4, sell=95.0, pnl=-20.0)]       # live: stopped -$20
    rows, tot = sb.score(t, {}, lambda tk, since: bars(RUNUP_THEN_FALL),
                         {"start_pct": 0.5, "cushion_pct": 0.5, "step_pct": 0.25})
    r = rows[0]
    assert r["status"] == "closed" and r["live"] == -20.0
    assert r["owner"] >= 0 and r["owner_open"] is False      # exited at/above BE
    assert r["diff"] == r["owner"] - r["live"] and tot["closed_n"] == 1


def test_open_trades_use_broker_unrealized_and_owner_marks_at_last_close():
    flat = [(100, 100.2, 99.9, 100.1)] * 3
    rows, tot = sb.score([trade("REGN", 100.0, 1)],
                         {"REGN": {"unrealized": 0.1}},
                         lambda tk, since: bars(flat), None)
    assert rows[0]["status"] == "open" and rows[0]["live"] == 0.1
    assert rows[0]["owner_open"] is True and abs(rows[0]["owner"] - 0.1) < 1e-9
    assert tot["closed_n"] == 0


def test_rule_needs_ten_closed_trades_and_owner_ahead():
    def run(n, live, owner_px):
        t = [trade(f"T{i}", 100.0, 1, sell=100 + live, pnl=live) for i in range(n)]
        flat = [(100, 100, 100, 100), (owner_px, owner_px, owner_px, owner_px)]
        return sb.score(t, {}, lambda tk, since: bars(flat), None)[1]
    assert run(9, -1.0, 100.5)["rule_met"] is False          # too few
    assert run(10, -1.0, 100.5)["rule_met"] is True          # owner ahead
    assert run(10, 1.0, 100.5)["rule_met"] is False          # live ahead


def test_a_trade_without_bars_is_listed_not_scored():
    rows, tot = sb.score([trade("XX", 100.0, 1)], {}, lambda tk, since: None, None)
    assert rows[0]["diff"] is None and tot["unscored"] == ["XX"]


def test_table_fits_the_phone_and_states_the_rule():
    rows, tot = sb.score([trade("TXN", 100.0, 4, sell=95.0, pnl=-20.0)], {},
                         lambda tk, since: bars(RUNUP_THEN_FALL), None)
    lines = sb.table_lines(rows, tot, date(2026, 10, 23))
    assert all(len(ln) <= trade_table.WIDTH for ln in lines)
    text = "\n".join(lines)
    assert "TXN" in text and "TOTAL" in text and "needs 9 more closed" in text


def test_only_account_daily_trades_are_scored(temp_journal):
    j = temp_journal
    for tk, setup, ts in (("OLD", "mean_reversion_reclaim", None),
                          ("TXN", "mean_reversion_reclaim", "x"),
                          ("TRND", "trend_continuation", "x")):
        did = j.log_decision(tk, setup, {"entry": 100, "stop": 95, "target": 115},
                             {"approved": True, "conviction_score": 80})
        j.log_trade(tk, "BUY", 1, 100.0, reason=setup, decision_id=did)
    conn = sqlite3.connect(j.DB_FILE)
    conn.execute("UPDATE trades SET timestamp='2026-10-09 09:41:00' WHERE ticker='OLD'")
    conn.commit()
    cfg = json.load(open("bot_config.json", encoding="utf-8"))
    names = [t["ticker"] for t in sb.daily_trades(conn, cfg)]
    assert names == ["TXN"]                         # pre-account and intraday left out
    t = sb.daily_trades(conn, cfg)[0]
    assert t["orig_stop"] == 95 and t["orig_target"] == 115


def test_posts_on_fridays_at_1630_through_the_job_wrapper():
    with open("jobs/macos/com.tradingbot.scoreboard.plist", "rb") as f:
        p = plistlib.load(f)
    assert p["ProgramArguments"][-1] == "scoreboard.py"
    assert p["StartCalendarInterval"] == [{"Weekday": 5, "Hour": 16, "Minute": 30}]


def test_main_posts_through_post_async(monkeypatch, discord_posts, tmp_path):
    monkeypatch.setattr(sb, "build", lambda *a, **k: ([], {
        "live": 0.0, "owner": 0.0, "closed_n": 0, "closed_live": 0.0,
        "closed_owner": 0.0, "closed_diff": 0.0, "rule_met": False, "unscored": []}))
    import broker
    monkeypatch.setattr(broker, "Broker", lambda: None)
    assert sb.main(["--date", "2026-10-23"]) == 0
    assert len(discord_posts) == 1
    assert "Weekly scoreboard" in discord_posts[0]["payload"]
