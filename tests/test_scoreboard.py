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


def trade(ticker, buy, qty, sell=None, pnl=None, stop=98.0, target=115.0,
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
    assert r["rule"] is not None
    assert r["diff"] == r["owner"] - r["rule"] and tot["closed_n"] == 1   # rules vs rules


def test_open_trades_use_broker_unrealized_and_owner_marks_at_last_close():
    flat = [(100, 100.2, 99.9, 100.1)] * 3
    rows, tot = sb.score([trade("REGN", 100.0, 1)],
                         {"REGN": {"unrealized": 0.1}},
                         lambda tk, since: bars(flat), None)
    assert rows[0]["status"] == "open" and rows[0]["live"] == 0.1
    assert rows[0]["owner_open"] is True and abs(rows[0]["owner"] - 0.1) < 1e-9
    assert rows[0]["rule_open"] is True and abs(rows[0]["rule"] - 0.1) < 1e-9
    assert tot["closed_n"] == 0


def test_rule_needs_ten_closed_trades_and_owner_ahead_of_the_rule():
    own = {"start_pct": 0.5, "cushion_pct": 0.5, "step_pct": 0.25}
    owner_wins = [(100, 100, 100, 100), (100, 100.9, 100, 100.8), (100.8, 100.8, 97, 97.5)]
    # a 0.7% dip after +1.1R: the owner ladder is shaken out near +0.3%, the
    # live rule (stop at BE) holds and runs to the 106 target
    rule_wins = [(100, 100, 100, 100), (100, 102.4, 100.1, 102.2),
                 (102.2, 102.2, 101.4, 101.5), (101.5, 106.5, 101.5, 106)]

    def run(n, seq):
        t = [trade(f"T{i}", 100.0, 1, sell=99.0, pnl=-1.0, target=106.0) for i in range(n)]
        return sb.score(t, {}, lambda tk, since: bars(seq, start="2026-10-14 14:00"), own)[1]
    assert run(9, owner_wins)["rule_met"] is False          # too few closed
    assert run(10, owner_wins)["rule_met"] is True          # owner ahead
    assert run(10, rule_wins)["rule_met"] is False          # live rule ahead
    assert run(10, rule_wins)["closed_diff"] < 0


def test_a_trade_without_bars_is_listed_not_scored():
    rows, tot = sb.score([trade("XX", 100.0, 1)], {}, lambda tk, since: None, None)
    assert rows[0]["diff"] is None and tot["unscored"] == ["XX"]


def test_table_fits_the_phone_and_states_the_rule():
    rows, tot = sb.score([trade("TXN", 100.0, 4, sell=95.0, pnl=-20.0)], {},
                         lambda tk, since: bars(RUNUP_THEN_FALL), None)
    lines = sb.table_lines(rows, tot, date(2026, 10, 23))
    assert all(len(ln) <= trade_table.WIDTH for ln in lines)
    text = "\n".join(lines)
    assert "TXN" in text and "TOTAL" in text and "9 more closed needed" in text
    assert lines[-1] == "CEO action: none"


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
        "live": 0.0, "rule": 0.0, "owner": 0.0, "closed_n": 0, "closed_rule": 0.0,
        "closed_owner": 0.0, "closed_diff": 0.0, "rule_met": False, "unscored": []}))
    import broker
    monkeypatch.setattr(broker, "Broker", lambda: None)
    assert sb.main(["--date", "2026-10-23"]) == 0
    assert len(discord_posts) == 1
    assert "Weekly scoreboard" in discord_posts[0]["payload"]


# ------------------------------------------------ CEO fixes 2026-10-21

def test_the_live_rule_replay_moves_the_stop_to_breakeven_at_plus_1r():
    """TXN shape: entry 100, stop 98 (R 2). A 10:00+ close at 102.2 (+1.1R)
    moves the live rule's stop to entry (S29); the fall exits at 100 = $0,
    where the old live result lost money."""
    import lock_replay
    import position_mgmt as pm
    b = bars([(100, 100, 100, 100), (100, 102.4, 100.1, 102.2),
              (101, 101, 99.0, 99.2)], start="2026-10-14 14:00")   # 10:00 ET
    px, ts, why = lock_replay.replay_r_atr(b, "2026-10-14 09:31:00", 100.0, 98.0,
                                           106.0, dict(pm.R_ATR_DEFAULTS))
    assert (px, why) == (100.0, "stop")


def test_the_live_rule_ignores_closes_before_10am_et():
    import lock_replay
    import position_mgmt as pm
    b = bars([(100, 100, 100, 100), (100, 102.4, 100.1, 102.2),
              (101, 101, 97.5, 98.0)], start="2026-10-14 13:35")   # 09:35 ET
    px, ts, why = lock_replay.replay_r_atr(b, "2026-10-14 09:31:00", 100.0, 98.0,
                                           106.0, dict(pm.R_ATR_DEFAULTS))
    assert (px, why) == (98.0, "stop")             # no ratchet before 10:00


def test_bug_affected_trades_get_a_dagger(temp_journal):
    j = temp_journal
    j.log_integrity_event("fill_corrected", "TXN SELL row 43: 278.78 -> 278.5325")
    conn = sqlite3.connect(j.DB_FILE)
    today = __import__("datetime").date.today().isoformat()
    trades = [dict(trade("TXN", 100.0, 4, sell=95.0, pnl=-20.0), buy_ts=today + " 09:31:00",
                   sell_ts=today + " 10:00"),
              dict(trade("REGN", 100.0, 1), buy_ts=today + " 09:31:00")]
    flags = sb.bug_flags(conn, trades)
    assert flags == {today + " 09:31:00"} or flags == {trades[0]["buy_ts"]}
    rows, tot = sb.score(trades, {"REGN": {"unrealized": 0.0}},
                         lambda tk, since: bars(RUNUP_THEN_FALL), None,
                         flags={trades[0]["buy_ts"]})
    lines = sb.table_lines(rows, tot, date(2026, 10, 23))
    assert lines[2].startswith("TXN†") and "† live result bent by a journaled bug" in lines


def test_rule_met_ends_with_ceo_action_switch():
    t = [trade(f"T{i}", 100.0, 1, sell=99.0, pnl=-1.0) for i in range(10)]
    # owner ladder locks +0.5% then exits on the fall; the live rule never
    # reaches +1R and rides the 98 stop -> owner ahead on every trade
    seq = [(100, 100, 100, 100), (100, 100.9, 100, 100.8), (100.8, 100.8, 97, 97.5)]
    rows, tot = sb.score(t, {}, lambda tk, since: bars(seq, start="2026-10-14 14:00"),
                         {"start_pct": 0.5, "cushion_pct": 0.5, "step_pct": 0.25})
    assert tot["closed_n"] == 10 and tot["closed_diff"] > 0 and tot["rule_met"]
    lines = sb.table_lines(rows, tot, date(2026, 10, 23))
    assert lines[-1] == "CEO action: switch" and "Rule MET (10 closed)." in lines


def test_the_scoreboard_never_edits_the_config():
    src = open("scoreboard.py", encoding="utf-8").read()
    assert '"w"' not in src and "json.dump" not in src
