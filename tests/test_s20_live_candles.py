"""S20 update (CEO 2026-10-23): live 1-minute candles from the IEX websocket
for the dashboard; 1m/5m/15m; display only, never in the worker."""

import pandas as pd
import pytest

import live_stream as ls

ET = "America/New_York"


def t(hhmm, sec=0, day="2026-10-23"):
    return pd.Timestamp(f"{day} {hhmm}:{sec:02d}", tz=ET)


def test_trades_build_the_live_minute_candle():
    lb = ls.LiveBars()
    for sec, px, sz in ((1, 100.0, 10), (20, 101.5, 5), (40, 99.5, 5), (59, 100.8, 1)):
        lb.on_trade("X", px, sz, t("10:00", sec))
    c = lb.candles("X", "1m", "2026-10-23")
    assert len(c) == 1
    row = c.iloc[0]
    assert (row.open, row.high, row.low, row.close, row.volume) == (100.0, 101.5, 99.5, 100.8, 21)
    assert row.ts == pd.Timestamp("2026-10-23 10:00")


def test_the_streamed_minute_bar_replaces_the_trade_built_one():
    lb = ls.LiveBars()
    lb.on_trade("X", 100.0, 1, t("10:00", 5))
    lb.on_bar("X", 100.1, 101, 99, 100.6, 500, t("10:00"))
    assert lb.candles("X")["close"].tolist() == [100.6]


def test_history_fills_gaps_but_never_overwrites_live_data():
    lb = ls.LiveBars()
    lb.on_bar("X", 1, 2, 0.5, 1.5, 10, t("10:01"))
    idx = pd.DatetimeIndex([t("10:00").tz_convert("UTC"), t("10:01").tz_convert("UTC")])
    hist = pd.DataFrame({"open": [9, 9], "high": [9, 9], "low": [9, 9], "close": [9, 9],
                         "volume": [1, 1]}, index=idx)
    lb.load_history("X", hist)
    assert lb.candles("X")["close"].tolist() == [9.0, 1.5]


def test_5m_and_15m_resample_the_minute_candles():
    lb = ls.LiveBars()
    for i in range(15):
        m = 30 + i
        lb.on_bar("X", 100 + i, 100.5 + i, 99.5 + i, 100.2 + i, 10, t(f"09:{m:02d}"))
    five = lb.candles("X", "5m")
    assert len(five) == 3
    assert (five.iloc[0].open, five.iloc[0].high, five.iloc[0].close, five.iloc[0].volume) == \
        (100, 104.5, 104.2, 50)
    assert len(lb.candles("X", "15m")) == 1


def test_only_todays_regular_session_is_drawn():
    lb = ls.LiveBars()
    lb.on_bar("X", 1, 1, 1, 1, 1, t("09:20"))                 # pre-market
    lb.on_bar("X", 2, 2, 2, 2, 1, t("10:00", day="2026-10-22"))
    lb.on_bar("X", 3, 3, 3, 3, 1, t("10:00"))
    assert lb.candles("X", "1m", "2026-10-23")["close"].tolist() == [3.0]


def test_never_in_the_worker(monkeypatch):
    monkeypatch.setenv("TRADINGBOT_WORKER", "1")
    with pytest.raises(RuntimeError, match="dashboard-only"):
        ls.LiveBars()


class FakeStream:
    def __init__(self):
        self.subs = []

    def subscribe_trades(self, h, *s):
        self.subs.append(("trades", s))

    def subscribe_bars(self, h, *s):
        self.subs.append(("bars", s))

    def subscribe_updated_bars(self, h, *s):
        self.subs.append(("updated", s))

    def run(self):
        import time
        time.sleep(3600)


class FakeBroker:
    def __init__(self):
        self.calls = 0

    def get_bars(self, tickers, timeframe_minutes=5, lookback_days=5):
        assert timeframe_minutes == 1
        self.calls += 1
        idx = pd.DatetimeIndex([t("10:00").tz_convert("UTC")])
        return {s: pd.DataFrame({"open": [5], "high": [6], "low": [4], "close": [5.5],
                                 "volume": [100]}, index=idx) for s in tickers}


def test_ensure_backfills_and_subscribes_once_per_symbol():
    st_ = FakeStream()
    lb = ls.LiveBars(FakeBroker(), lambda: st_)
    lb.ensure(["AAA", "BBB"])
    lb.ensure(["AAA"])
    assert lb.candles("AAA")["close"].tolist() == [5.5]
    assert ("trades", ("AAA", "BBB")) in st_.subs and len(st_.subs) == 3


def test_rest_fallback_polls_when_the_stream_is_down():
    b = FakeBroker()
    lb = ls.LiveBars(b, stream_factory=None)
    lb.ensure(["AAA"])
    lb.poll(["AAA"])
    assert b.calls == 2 and not lb.streaming


def test_candle_chart_builds_with_levels():
    import dashboard_charts as dc
    lb = ls.LiveBars()
    lb.on_bar("X", 100, 101, 99, 100.5, 1, t("10:00"))
    lb.on_bar("X", 100.5, 100.6, 99.8, 100.0, 1, t("10:01"))
    spec = dc.candle_chart({"ticker": "X", "status": "open", "entry": 100.0,
                            "stop": 98.0, "target": 106.0}, lb.candles("X"), "1m").to_dict()
    marks = [l["mark"]["type"] if isinstance(l["mark"], dict) else l["mark"]
             for l in spec["layer"]]
    assert "bar" in marks and "rule" in marks and "1m" in spec["title"]


def test_dashboard_wires_a_5_second_fragment_default_1m_and_keeps_the_worker_on_5m():
    src = open("streamlit_app.py", encoding="utf-8").read()
    assert "@st.fragment(run_every=5)\ndef _render_live_candles" in src
    assert 'st.segmented_control("Candles", ["1m", "5m", "15m"], default="1m"' in src
    start = src.index("def _worker_loop")
    loop = src[start:src.index("\ndef ", start + 10)]           # the worker loop only
    assert "live_stream" not in loop and "_live_bars" not in loop
    assert 'interval_mins = config["interval"]' in src          # decisions untouched
