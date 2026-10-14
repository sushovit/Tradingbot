"""Live-tab charts: the frames behind them (display only, no network)."""

from datetime import datetime

import pandas as pd
import pytest

import dashboard_charts as dc


def daily(closes, start="2026-09-01"):
    idx = pd.bdate_range(start, periods=len(closes), tz="UTC") + pd.Timedelta(hours=4)
    return pd.DataFrame({"open": closes, "high": closes, "low": closes,
                         "close": closes, "volume": 1}, index=idx)


def test_trend_frame_marks_chop_the_live_way():
    spy = daily([100.0] * 30 + [90.0] + [120.0])
    df = dc.spy_trend_frame(spy, sessions=5)
    assert len(df) == 5
    assert bool(df["chop"].iloc[-2]) is True      # 90 below its EMA
    assert bool(df["chop"].iloc[-1]) is False     # 120 above it
    # EMA warmed on the full series, not the 5-row tail
    full = spy["close"].ewm(span=20, adjust=False).mean().iloc[-1]
    assert df["ema20"].iloc[-1] == pytest.approx(full)


def test_progress_frame_indexes_both_to_100_on_the_takeover_day():
    spy = daily([700.0, 770.0, 777.0, 784.7], start="2026-10-08")
    eq = [("2026-10-08", 0.0), ("2026-10-09", 5000.0), ("2026-10-10", 5000.0),
          ("2026-10-12", 5050.0), ("2026-10-13", 4950.0)]
    df = dc.progress_frame(eq, spy)
    acct = df[df["series"] == "$5,000 account"]
    assert list(acct["date"].dt.strftime("%m-%d")) == ["10-09", "10-12", "10-13"]
    assert list(acct["index"].round(2)) == [100.0, 101.0, 99.0]
    s = df[df["series"] == "SPY"]
    assert s["index"].iloc[0] == 100.0 and s["index"].iloc[-1] == pytest.approx(101.909, abs=1e-3)
    assert dc.progress_frame([], daily([1.0], start="2026-10-09")).shape[0] == 1


def bars_for(session="2026-10-13", start=100.0, step=0.5, n=10):
    idx = pd.date_range(f"{session} 09:30", periods=n, freq="5min",
                        tz="America/New_York").tz_convert("UTC")
    pre = pd.date_range(f"{session} 08:00", periods=3, freq="5min",
                        tz="America/New_York").tz_convert("UTC")
    closes = [start + step * i for i in range(n)]
    df = pd.DataFrame({"close": [1.0] * 3 + closes}, index=pre.append(idx))
    return df


def test_selected_items_positions_first_then_newest_candidates():
    bars = {t: bars_for() for t in ("MU", "AMT", "NOW", "FANG")}
    cands = [
        {"ts": "2026-10-13 09:32:00", "ticker": "AMT", "setup": "r",
         "approved": False, "entry": 100.0, "stop": 98.0, "target": 106.0},
        {"ts": "2026-10-13 09:47:00", "ticker": "FANG", "setup": "t",
         "approved": True, "entry": 102.0, "stop": 101.0, "target": 105.0},
        {"ts": "2026-10-13 09:50:00", "ticker": "MU", "setup": "t",
         "approved": False, "entry": 1.0, "stop": 0.5, "target": 2.0},
    ]
    items = dc.selected_items({"MU": {"entry": 100.0, "stop": 95.0,
                                      "target": 115.0, "setup": "r"}},
                              cands, bars, "2026-10-13", limit=3)
    assert [(i["ticker"], i["status"]) for i in items] == [
        ("MU", "open"), ("FANG", "approved"), ("AMT", "rejected")]
    assert len(items[0]["bars"]) == 10            # regular hours only
    assert items[0]["change_pct"] == pytest.approx(4.5)   # 104.5 vs 100
    assert dc.title_for(items[1]) == "FANG · gatekeeper approved · +2.45% vs entry"


def test_every_chart_builds_to_a_valid_spec():
    spy = daily([700.0 + i for i in range(40)], start="2026-09-01")
    dc.market_chart(dc.spy_trend_frame(spy)).to_dict()
    dc.progress_chart(dc.progress_frame(
        [("2026-10-09", 5000.0), ("2026-10-12", 5010.0)],
        daily([1.0, 1.01], start="2026-10-09"))).to_dict()
    item = {"ticker": "NOW", "status": "rejected", "setup": "t",
            "entry": 101.0, "stop": 100.0, "target": 104.0,
            "ts": "2026-10-13 09:40:00", "bars": dc.session_frame(bars_for(), "2026-10-13"),
            "change_pct": 1.0}
    spec = dc.selected_chart(item).to_dict()
    texts = str(spec)
    assert "stop $100.00" in texts and "target $104.00" in texts
    assert "signal 09:40" in texts
