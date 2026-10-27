"""S56b: 1.5 x ATR target twin of live positions - paper only, no network."""

import json
import sqlite3

import pandas as pd
import pytz

import cap_shadow as cs

ET = pytz.timezone("America/New_York")


def _bars(day, rows, start="09:30"):
    idx = pd.date_range(f"{day} {start}", periods=len(rows), freq="5min", tz="America/New_York")
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"])


def test_twin_target_is_the_live_formula_with_a_1_5_cap():
    # IBM 10-23: fill 250.42, initial stop 239.99 (R 10.43), ATR14 6.9525
    assert cs.twin_target(250.42, 239.99, 6.9525) == 260.85        # 1.5 x ATR < 3R
    assert cs.twin_target(100.0, 99.0, 10.0) == 103.0               # 3R < 1.5 x ATR


def test_twin_sells_at_its_target_when_touched_first():
    b = _bars("2026-10-27", [(100, 100.5, 99.8, 100.2), (100.2, 101.2, 100.1, 101.0),
                             (101.0, 103.1, 100.9, 102.5)])
    r = cs.evaluate(b, "2026-10-27 09:31:10", 100.0, 10, 103.0)
    assert r["status"] == cs.TWIN_TARGET and r["twin_exit_px"] == 103.0 and r["twin_pnl"] == 30.0


def test_a_high_in_the_entry_bar_does_not_count():
    b = _bars("2026-10-27", [(100, 104.0, 99.8, 100.2), (100.2, 100.4, 100.1, 100.3)])
    assert cs.evaluate(b, "2026-10-27 09:31:10", 100.0, 10, 103.0)["status"] == cs.OPEN


def test_live_exit_first_means_the_same_exit():
    b = _bars("2026-10-27", [(100, 100.5, 99.8, 100.2), (100.2, 100.4, 98.0, 98.5),
                             (98.5, 104.0, 98.4, 103.5)])                 # touches only after
    r = cs.evaluate(b, "2026-10-27 09:31:10", 100.0, 10, 103.0,
                    live_exit_ts="2026-10-27 09:36:00", live_exit_px=98.2)
    assert r["status"] == cs.SAME and r["twin_exit_px"] == 98.2 and r["twin_pnl"] == -18.0


def test_live_exit_above_the_twin_target_counts_as_a_twin_target_hit():
    b = _bars("2026-10-27", [(100, 100.5, 99.8, 100.2), (100.2, 108.0, 100.1, 107.5)])
    r = cs.evaluate(b, "2026-10-27 09:31:10", 100.0, 10, 103.0,
                    live_exit_ts="2026-10-27 09:38:00", live_exit_px=107.0)
    assert r["status"] == cs.TWIN_TARGET and r["twin_exit_px"] == 103.0


class FakeData:
    def __init__(self, m5):
        self._m = m5

    def m5(self, tickers, start_day):
        return {t: self._m[t] for t in tickers if t in self._m}


def test_update_registers_live_positions_from_their_state_and_never_trades(tmp_path):
    db = str(tmp_path / "j.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, timestamp TEXT, ticker TEXT, "
                 "action TEXT, qty REAL, price REAL)")
    conn.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?)", [
        (40, "2026-10-20 09:32:00", "OLD", "BUY", 1, 50.0),
        (41, "2026-10-21 10:00:00", "OLD", "SELL", 1, 49.0),          # closed before START
        (49, "2026-10-23 09:32:54", "IBM", "BUY", 4, 250.42)])         # open on START
    conn.commit()
    conn.close()
    pos = tmp_path / "positions.json"
    pos.write_text(json.dumps({"IBM": {"in_position": True, "entry_trade_id": 49, "timeframe": "daily",
                                       "original_stop": 239.99, "target_cap_atr14": 6.9525,
                                       "profit_target_price": 271.28}}))
    c = cs.connect(db)
    bars = {"IBM": pd.concat([_bars("2026-10-23", [(250.4, 251.0, 249.9, 250.8)] * 3),
                              _bars("2026-10-27", [(252.0, 261.0, 251.8, 259.0)])])}
    now = ET.localize(pd.Timestamp("2026-10-27 10:00").to_pydatetime())
    cs.update(c, FakeData(bars), now, positions_file=str(pos))
    (r,) = cs.rows(c)
    assert (r["ticker"], r["twin_target"], r["live_target"]) == ("IBM", 260.85, 271.28)
    assert r["status"] == cs.TWIN_TARGET and r["twin_pnl"] == round(4 * (260.85 - 250.42), 2)
    assert r["live_pnl"] == round(4 * (259.0 - 250.42), 2) and r["source"] == "cap_shadow"
    assert "diff $" in cs.summary_line(cs.rows(c))
    assert c.execute("SELECT count(*) FROM trades").fetchone()[0] == 3     # untouched


def test_cap_shadow_has_no_order_path_and_the_worker_never_imports_it():
    src = open("cap_shadow.py", encoding="utf-8").read()
    for bad in ("submit", "OrderRequest", "replace_order", "close_position", "cancel_order",
                "INSERT INTO trades", "INSERT INTO decisions", "safe_io"):
        assert bad not in src, bad
    worker = open("streamlit_app.py", encoding="utf-8").read()
    assert worker.count("import cap_shadow") == 1
    i = worker.index("import cap_shadow")
    assert worker.rfind("with tab5:", 0, i) > worker.rfind("def live_bot_worker", 0, i)
