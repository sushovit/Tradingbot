"""S56b: the OLD 3.0 x ATR cap twin of live positions (live cap 1.5 from
2026-10-28) - paper only, no network."""

import json
import sqlite3

import pandas as pd
import pytz

import cap_shadow as cs

ET = pytz.timezone("America/New_York")
STEPS = [(1.0, 0.0), (1.5, 0.0), (2.0, 1.0)]


def _bars(day, rows, start="09:30"):
    idx = pd.date_range(f"{day} {start}", periods=len(rows), freq="5min", tz="America/New_York")
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"])


def ROW(**kw):
    r = {"fill": 100.0, "qty": 10.0, "initial_stop": 98.0, "live_target": 103.0,
         "twin_target": 106.0, "entry_ts": "2026-10-28 10:01:10"}
    r.update(kw)
    return r


def LIVE(ts=None, px=None, reason=None, stop=None):
    return {"live_exit_ts": ts, "live_exit_px": px, "live_exit_reason": reason, "live_exit_stop": stop}


def test_twin_target_is_the_live_formula_with_the_old_3_0_cap():
    # IBM 10-23 geometry: fill 250.42, stop 239.99 (R 10.43), ATR14 6.9525
    assert cs.twin_target(250.42, 239.99, 6.9525) == 271.28           # 3.0 x ATR < 3R
    assert cs.twin_target(100.0, 99.0, 10.0) == 103.0                  # 3R binds
    assert (cs.CAP_ATR, cs.START, cs.REVIEW_AFTER) == (3.0, "2026-10-28", 20)


def test_open_live_position_is_the_same_trade():
    b = _bars("2026-10-28", [(100, 101, 99.5, 100.5)] * 9)
    r = cs.evaluate(b, {}, {}, ROW(), LIVE(), STEPS)
    assert r["status"] == cs.OPEN and r["mark_px"] == 100.5


def test_live_stop_or_time_exit_means_the_same_exit():
    b = _bars("2026-10-28", [(100, 100.5, 97.9, 98.0)] * 9)
    r = cs.evaluate(b, {}, {}, ROW(), LIVE("2026-10-28 10:12:00", 98.0, "Stop Loss", 98.0), STEPS)
    assert r["status"] == cs.SAME and r["twin_exit_px"] == 98.0


def test_after_the_live_target_the_twin_runs_on_to_the_old_target():
    rows = ([(100, 100.4, 99.8, 100.2)] * 7            # 09:30-10:00
            + [(100.2, 103.2, 100.1, 102.9)]           # 10:05: live sells at 103
            + [(102.9, 104.0, 102.5, 103.8), (103.8, 106.3, 103.6, 106.0)])   # 3.0 target 106
    b = _bars("2026-10-28", rows)
    r = cs.evaluate(b, {}, {}, ROW(), LIVE("2026-10-28 10:06:30", 103.0, "Profit Target", 100.0),
                    STEPS)
    assert r["status"] == cs.CLOSED and r["twin_exit_px"] == 106.0
    assert r["twin_exit_ts"].startswith("2026-10-28 10:15")


def test_after_the_live_target_the_twin_can_give_it_back_at_the_ladder_stop():
    rows = ([(100, 100.4, 99.8, 100.2)] * 7
            + [(100.2, 103.1, 100.1, 102.5)]           # live target 103 hit; hc 102.5 = +1.25R
            + [(102.4, 102.6, 101.0, 101.2), (101.2, 101.3, 99.5, 99.8)])
    b = _bars("2026-10-28", rows)
    r = cs.evaluate(b, {}, {}, ROW(), LIVE("2026-10-28 10:06:30", 103.0, "Profit Target", None),
                    STEPS)
    assert r["status"] == cs.CLOSED and r["twin_exit_px"] == 100.0      # +1R step -> breakeven


def test_twin_still_running_after_the_live_target():
    rows = [(100, 100.4, 99.8, 100.2)] * 7 + [(100.2, 103.1, 100.1, 102.9), (102.9, 104, 102.6, 103.5)]
    b = _bars("2026-10-28", rows)
    r = cs.evaluate(b, {}, {}, ROW(), LIVE("2026-10-28 10:06:30", 103.0, "Profit Target (synced)", 100.0),
                    STEPS)
    assert r["status"] == cs.RUNNING and r["mark_px"] == 103.5


def test_when_3r_binds_both_targets_are_equal_and_the_exit_is_the_same():
    b = _bars("2026-10-28", [(100, 100.4, 99.8, 100.2)] * 9)
    r = cs.evaluate(b, {}, {}, ROW(live_target=103.0, twin_target=103.0),
                    LIVE("2026-10-28 10:06:30", 103.0, "Profit Target", 100.0), STEPS)
    assert r["status"] == cs.SAME


class FakeData:
    def __init__(self, m5, daily=None):
        self._m, self._d = m5, daily or {}

    def m5(self, tickers, start_day):
        return {t: self._m[t] for t in tickers if t in self._m}

    def daily(self, tickers, days=10):
        return {t: self._d[t] for t in tickers if t in self._d}


def test_update_registers_new_entries_from_the_live_state_and_never_trades(tmp_path):
    db = str(tmp_path / "j.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, timestamp TEXT, ticker TEXT, "
                 "action TEXT, qty REAL, price REAL, reason TEXT, exit_stop REAL)")
    conn.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?)", [
        (49, "2026-10-23 09:32:54", "IBM", "BUY", 4, 250.42, "x", None),      # before START
        (60, "2026-10-28 10:01:10", "AAA", "BUY", 10, 100.0, "x", None),
        (61, "2026-10-28 10:06:30", "AAA", "SELL", 10, 103.0, "Profit Target", 100.0)])
    conn.commit()
    conn.close()
    pos = tmp_path / "positions.json"
    pos.write_text(json.dumps({"AAA": {"in_position": True, "entry_trade_id": 60, "timeframe": "daily",
                                       "original_stop": 98.0, "target_cap_atr14": 2.0,
                                       "profit_target_price": 103.0}}))
    c = cs.connect(db)
    rows = ([(100, 100.4, 99.8, 100.2)] * 7 + [(100.2, 103.2, 100.1, 102.9)]
            + [(102.9, 104.0, 102.5, 103.8), (103.8, 106.3, 103.6, 106.0)])
    now = ET.localize(pd.Timestamp("2026-10-28 10:20").to_pydatetime())
    cs.update(c, FakeData({"AAA": _bars("2026-10-28", rows)}), now, positions_file=str(pos))
    (r,) = cs.rows(c)
    assert (r["ticker"], r["live_target"], r["twin_target"]) == ("AAA", 103.0, 106.0)
    assert r["status"] == cs.CLOSED and r["live_pnl"] == 30.0 and r["twin_pnl"] == 60.0
    assert r["source"] == "cap_shadow"
    line = cs.summary_line(cs.rows(c))
    assert "3.0 - live $+30.00" in line and "review at 1/20 closed" in line
    assert c.execute("SELECT count(*) FROM trades").fetchone()[0] == 3     # untouched


def test_review_is_flagged_at_20_closed():
    rs = [{"status": cs.CLOSED, "live_pnl": 1.0, "twin_pnl": 2.0}] * 20
    assert "REVIEW DUE (20 closed)" in cs.summary_line(rs)


def test_cap_shadow_has_no_order_path_and_the_worker_never_imports_it():
    src = open("cap_shadow.py", encoding="utf-8").read()
    for bad in ("submit", "OrderRequest", "replace_order", "close_position", "cancel_order",
                "INSERT INTO trades", "INSERT INTO decisions", "safe_io"):
        assert bad not in src, bad
    worker = open("streamlit_app.py", encoding="utf-8").read()
    assert worker.count("import cap_shadow") == 1
    i = worker.index("import cap_shadow")
    assert worker.rfind("with tab5:", 0, i) > worker.rfind("def live_bot_worker", 0, i)


# ----------------------------------------------------------------- weekly scoreboard column

def _sb_row(ticker, buy_ts, live, status="closed"):
    return {"ticker": ticker, "buy_ts": buy_ts, "status": status, "live": live, "rule": live,
            "owner": live, "diff": 0.0, "rule_open": False, "owner_open": False, "bug": False}


def test_scoreboard_gets_a_3_0_cap_column(tmp_path):
    import scoreboard as sb
    c = cs.connect(str(tmp_path / "j.db"))
    c.execute("INSERT INTO cap_shadow (trade_id, ticker, entry_ts, fill, qty, status, live_pnl, "
              "twin_pnl) VALUES (60, 'AAA', '2026-10-28 10:01:10', 100, 10, 'twin_closed', 30, 60)")
    rows = [_sb_row("IBM", "2026-10-23 09:32:54", -0.96),
            _sb_row("AAA", "2026-10-28 10:01:10", 30.0),
            _sb_row("BBB", "2026-10-29 10:00:00", 5.0, status="open")]
    sb.attach_cap3(rows, c)
    assert [r["cap3_mark"] for r in rows] == ["=", "", ""] and rows[1]["cap3"] == 60
    totals = {"live": 34.04, "rule": 34.04, "owner": 34.04, "closed_n": 2, "closed_diff": 0,
              "unscored": [], "rule_met": False}
    lines = sb.table_lines(rows, totals, None)
    assert lines[0].endswith("3.0CAP")
    assert lines[2].rstrip().endswith("=") and lines[3].rstrip().endswith("+60.00")
    assert any(ln.startswith("3.0 cap - live since 10-28: +30.00 (1 tr)") for ln in lines)
    assert "3.0 cap review at 1/20 closed" in lines
    assert max(len(ln) for ln in lines) <= 54
