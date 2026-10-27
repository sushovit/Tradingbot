"""CEO 2026-10-27 - S55 dip forward shadow test: paper only, no network."""

import re
import sqlite3
from datetime import date

import pandas as pd
import pytz

import dip_shadow as ds

ET = pytz.timezone("America/New_York")


def _day(d, rows, start="09:30"):
    """5-minute RTH bars for day `d` from (open, high, low, close) tuples."""
    idx = pd.date_range(f"{d} {start}", periods=len(rows), freq="5min", tz="America/New_York")
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"])


def _flat(d, px, n=78, start="09:30"):
    return _day(d, [(px, px + 0.1, px - 0.1, px)] * n, start)


# ----------------------------------------------------------------- the rule

def test_qualifies_price_and_open_band():
    assert ds.qualifies(100.0, 100.9) and ds.qualifies(100.0, 99.0)
    assert not ds.qualifies(100.0, 101.2)            # opened > +1%
    assert not ds.qualifies(49.0, 49.0)              # under $50
    assert ds.level_for(100.0) == 98.0


def test_first_touch_must_start_before_1100():
    early = _day("2026-10-28", [(100, 100.2, 99.5, 99.8)] * 6 + [(99, 99.1, 97.9, 98.5)])
    assert ds.find_touch(early, 98.0).strftime("%H:%M") == "10:00"
    late = _day("2026-10-28", [(100, 100.2, 99.5, 99.8)] * 18 + [(99, 99.1, 97.0, 98.5)])
    assert late.index[18].strftime("%H:%M") == "11:00"
    assert ds.find_touch(late, 98.0) is None


def test_control_is_the_0945_open():
    b = _day("2026-10-28", [(100 + i, 101 + i, 99 + i, 100 + i) for i in range(5)])
    ts, px = ds.control_price(b)
    assert ts.strftime("%H:%M") == "09:45" and px == 103


# ----------------------------------------------------------------- outcomes

def test_same_day_hit_exits_at_prior_close_with_worst_drawdown():
    rows = ([(100, 100.2, 99.6, 99.8)] * 3                 # 09:30-09:40
            + [(99.5, 99.6, 99.4, 99.5)]                    # 09:45 control open 99.5
            + [(99.0, 99.1, 97.5, 98.2)]                    # 09:50 touch (low 97.5)
            + [(98.2, 98.9, 97.2, 98.8)]                    # new low 97.2
            + [(98.8, 100.3, 98.7, 100.1)])                 # back to 100
    b = _day("2026-10-28", rows)
    touch = ds.find_touch(b, 98.0)
    r = ds.evaluate(b, touch, 98.0, 100.0, control_px=99.5, complete_through=None)
    assert r["status"] == ds.HIT0 and r["exit_px"] == 100.0
    assert r["ret_pct"] == round(100 * (100 / 98 - 1), 3)
    assert r["worst_dd_pct"] == round(100 * (97.2 / 98 - 1), 3)
    assert r["control_ret_pct"] == round(100 * (100 / 99.5 - 1), 3)


def test_a_high_inside_the_touch_bar_is_not_a_hit():
    b = _day("2026-10-28", [(100, 100.1, 99.8, 99.9), (99.9, 100.5, 97.8, 98.5)])
    r = ds.evaluate(b, b.index[1], 98.0, 100.0, complete_through=None)
    assert r["status"] == ds.OPEN


def test_no_hit_marks_at_the_day3_close_only_when_day3_is_over():
    days = ["2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02"]
    b = pd.concat([_day(days[0], [(100, 100.1, 99.8, 99.9), (99, 99.1, 97.9, 98.0)])]
                  + [_flat(d, 97.0) for d in days[1:]])
    entry = b.index[1]
    still = ds.evaluate(b, entry, 98.0, 100.0, complete_through=date(2026, 10, 30))
    assert still["status"] == ds.OPEN and still["sessions"] == 4
    done = ds.evaluate(b, entry, 98.0, 100.0, complete_through=date(2026, 11, 2))
    assert done["status"] == ds.DAY3 and done["exit_px"] == 97.0
    assert done["exit_ts"].startswith("2026-11-02 15:5")


def test_hit_on_a_later_session_is_hit_3d():
    b = pd.concat([_day("2026-10-28", [(100, 100.1, 99.8, 99.9), (99, 99.1, 97.9, 98.0)]),
                   _day("2026-10-29", [(98, 98.5, 97.8, 98.4), (98.4, 100.2, 98.3, 100.0)])])
    r = ds.evaluate(b, b.index[1], 98.0, 100.0, complete_through=None)
    assert r["status"] == ds.HIT3 and r["exit_ts"].startswith("2026-10-29")


# ----------------------------------------------------------------- update loop

class FakeData:
    def __init__(self, daily, m5):
        self._d, self._m = daily, m5

    def daily(self, tickers, days=10):
        return {t: self._d[t] for t in tickers if t in self._d}

    def m5(self, tickers, start_day):
        return {t: self._m[t] for t in tickers if t in self._m}


def test_update_finds_the_dip_tracks_it_and_never_touches_decisions(tmp_path):
    db = str(tmp_path / "j.db")
    conn = ds.connect(db)
    conn.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY)")
    daily = {"AAA": pd.DataFrame({"close": [100.0]}, index=pd.DatetimeIndex(
                 ["2026-10-27"], tz="America/New_York")),
             "CHEAP": pd.DataFrame({"close": [20.0]}, index=pd.DatetimeIndex(
                 ["2026-10-27"], tz="America/New_York")),
             "GAP": pd.DataFrame({"close": [100.0]}, index=pd.DatetimeIndex(
                 ["2026-10-27"], tz="America/New_York"))}
    rows = ([(100.2, 100.3, 99.8, 99.9)] * 3 + [(99.6, 99.7, 99.4, 99.5)]
            + [(99.0, 99.1, 97.6, 98.1), (98.1, 100.4, 98.0, 100.2)])
    m5 = {"AAA": _day("2026-10-28", rows), "GAP": _flat("2026-10-28", 103.0, 6)}
    data = FakeData(daily, m5)
    now = ET.localize(pd.Timestamp("2026-10-28 10:05").to_pydatetime())
    out = ds.update(now, data, conn, symbols=["AAA", "CHEAP", "GAP"])
    assert out["watch"] == 1 and out["new"] == 1          # GAP opened +3%, CHEAP < $50
    (r,) = ds.rows(conn)
    assert (r["ticker"], r["level"], r["status"], r["control_px"]) == ("AAA", 98.0, ds.HIT0, 99.6)
    assert r["source"] == "dip_shadow"
    assert ds.update(now, data, conn, symbols=["AAA"])["new"] == 0       # idempotent
    assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
    t = ds.totals(ds.rows(conn))
    assert t["closed"] == 1 and t["hit_same_day_pct"] == 100.0
    lines = ds.summary_lines(ds.rows(conn), "2026-10-28")
    assert lines[0].startswith("🧪 Dip test (S55, PAPER - no orders) 2026-10-28: 1 new dip(s): AAA @98.00")


def test_outside_the_window_no_new_dips(tmp_path):
    conn = ds.connect(str(tmp_path / "j.db"))
    now = ET.localize(pd.Timestamp("2026-11-09 10:05").to_pydatetime())
    assert ds.update(now, FakeData({}, {}), conn, symbols=["AAA"]) == {"watch": 0, "new": 0,
                                                                       "updated": 0}


# ----------------------------------------------------------------- never tradable

def test_dip_shadow_has_no_order_path():
    src = open("dip_shadow.py", encoding="utf-8").read()
    for bad in ("submit", "OrderRequest", "replace_order", "close_position", "cancel_order",
                "place_order", "INTO decisions", "INTO trades", "positions.json"):
        assert bad not in src, bad
    worker = open("streamlit_app.py", encoding="utf-8").read()
    # only the dashboard tab imports it, behind the in-worker guard
    assert len(re.findall(r"import dip_shadow", worker)) == 1
    assert "with tab5:\n    if not desk_config.in_worker():\n        import sqlite3\n        import dip_shadow as _ds" in worker
