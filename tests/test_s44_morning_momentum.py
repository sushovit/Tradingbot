"""S44 - morning-momentum (gap-and-go / opening-range breakout) replay on
catalyst names: candidate selection, opening range, entry trigger, fill and
exit rules, stats and the bar. Synthetic data only, no network (the Finnhub
client is a fake)."""

from datetime import date

import pandas as pd
import pytest

import study_exit_grid as g

DAY = "2026-07-15"


def session(day=DAY, overrides=None, base=100.0):
    """78 flat 5-minute RTH bars at `base`; overrides {"HH:MM": (o, h, l, c)}."""
    idx = pd.date_range(f"{day} 09:30", periods=78, freq="5min", tz=g.ET)
    rows = [(base, base, base, base)] * 78
    df = pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"])
    df["volume"] = 1000
    for hm, (o, h, l, c) in (overrides or {}).items():
        df.loc[pd.Timestamp(f"{day} {hm}", tz=g.ET), ["open", "high", "low", "close"]] = (o, h, l, c)
    return df


OR = {"09:30": (100, 101, 99, 100.5), "09:35": (100.5, 101.5, 99.5, 101),
      "09:40": (101, 102, 100, 101)}       # OR high 102, low 99


def test_opening_range_uses_the_first_three_bars_only():
    bars = session(overrides={**OR, "09:45": (101, 110, 90, 101)})
    assert g.s44_opening_range(bars) == (102.0, 99.0)
    assert g.s44_opening_range(bars.iloc[:0]) is None


def test_trigger_is_first_close_above_or_high_from_0945():
    bars = session(overrides={**OR, "09:45": (101, 103, 101, 101.9),
                              "09:50": (101.9, 103, 101.5, 102.5)})
    i = g.s44_trigger(bars, 102.0)
    assert bars.index[i].strftime("%H:%M") == "09:50"


def test_trigger_ignores_or_bars_and_after_1055():
    bars = session(overrides={"09:30": (100, 105, 99, 104.9), "11:00": (101, 106, 101, 105)})
    assert g.s44_trigger(bars, 102.0) is None
    bars = session(overrides={"10:55": (101, 106, 101, 105)})
    assert bars.index[g.s44_trigger(bars, 102.0)].strftime("%H:%M") == "10:55"


def test_trade_fills_next_open_target_2r_hit():
    # trigger 09:50 close 102.5; fill 10:00? no - next bar 09:55 open 102.5
    bars = session(overrides={**OR, "09:50": (101, 103, 101, 102.5),
                              "09:55": (103, 103.5, 102.8, 103.2),
                              "10:30": (103.2, 112, 103, 111)})
    t = g.s44_trade(bars, DAY, 2.0)
    assert t["status"] == "traded"
    assert t["entry"] == 103 and t["stop"] == 99 and t["target"] == 111
    assert t["r"] == pytest.approx(2.0) and t["why"] == "target"
    assert t["risk_pct"] == pytest.approx(400 / 103)
    assert t["t_entry"].strftime("%H:%M") == "09:55"


def test_stop_hit_and_spanning_bar_takes_stop():
    bars = session(overrides={**OR, "09:50": (101, 103, 101, 102.5),
                              "09:55": (103, 103.5, 102.8, 103.2),
                              "10:30": (103, 120, 90, 100)})
    t = g.s44_trade(bars, DAY, 2.0)
    assert t["why"] == "stop" and t["r"] == pytest.approx(-1.0)


def test_gap_through_stop_fills_at_open():
    bars = session(overrides={**OR, "09:50": (101, 103, 101, 102.5),
                              "09:55": (103, 103.5, 102.8, 103.2),
                              "10:00": (97, 98, 96, 97)})
    t = g.s44_trade(bars, DAY, 2.0)
    assert t["why"] == "stop (gap)" and t["r"] == pytest.approx((97 - 103) / 4)


def test_time_exit_at_1555_open():
    bars = session(overrides={**OR, "09:50": (101, 103, 101, 102.5),
                              "09:55": (103, 103.5, 102.8, 103.2),
                              "09:45": (101, 101, 101, 101),
                              "15:50": (104, 104, 104, 104),
                              "15:55": (105, 106, 104, 105)}, base=104.0)
    t = g.s44_trade(bars, DAY, 2.0)
    assert t["why"] == "time 15:55" and t["r"] == pytest.approx((105 - 103) / 4)


def test_skip_when_fill_open_above_planned_target():
    # trigger close 102.5, planned 2R target = 102.5 + 2 * 3.5 = 109.5
    bars = session(overrides={**OR, "09:50": (101, 103, 101, 102.5),
                              "09:55": (110, 111, 109, 110)})
    assert g.s44_trade(bars, DAY, 2.0)["status"] == "above_target"
    assert g.s44_trade(bars, DAY, 3.0)["status"] == "traded"


def test_skip_when_no_risk_and_no_trigger():
    bars = session(overrides={**OR, "09:50": (101, 103, 101, 102.5),
                              "09:55": (98.5, 99, 98, 98.5)})
    assert g.s44_trade(bars, DAY)["status"] == "no_risk"
    assert g.s44_trade(session(overrides=OR), DAY)["status"] == "no_trigger"
    assert g.s44_trade(session(), "2026-07-16")["status"] == "no_bars"


def daily(rows, start="2026-03-02"):
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame([{"open": o, "high": max(o, c), "low": min(o, c),
                          "close": c, "volume": v} for o, c, v in rows], index=idx)


def test_candidates_long_only_with_price_and_adv():
    rows = [(10, 10, 3_000_000)] * 20
    up = daily(rows + [(10.4, 10.5, 1)])          # +4%, ADV $30M
    down = daily(rows + [(9.5, 9.5, 1)])          # -5%: long side only
    small = daily(rows + [(10.29, 10.3, 1)])      # +2.9%
    thin = daily([(10, 10, 100)] * 20 + [(11, 11, 1)])
    cheap = daily([(4, 4, 10_000_000)] * 20 + [(4.5, 4.5, 1)])
    frames = {s: (None, d) for s, d in
              (("UP", up), ("DN", down), ("SM", small), ("TH", thin), ("CH", cheap))}
    c = g.s44_candidates(frames, start="2026-03-01")
    assert [(s, round(x, 3)) for d, v in c.items() for s, x in v] == [("UP", 0.04)]


def test_cap_keeps_top_15_by_gap_with_news():
    d = date(2026, 7, 15)
    cands = {d: [(f"S{i:02d}", 0.03 + i / 100) for i in range(20)]}
    picks = g.s36_cap(cands, lambda s, dd: s != "S19")
    assert len(picks) == 15 and ("S19", d) not in picks and ("S18", d) in picks
    assert ("S03", d) not in picks


def test_spy_trend_uses_previous_session():
    idx = pd.bdate_range("2026-03-02", periods=30)
    closes = [100.0] * 25 + [120.0, 120, 80, 80, 80]
    spy = pd.DataFrame({"close": closes}, index=idx)
    m = g.s44_spy_trend(spy)
    assert m[idx[25].date()] is False         # day 25's own jump not yet known
    assert m[idx[26].date()] is True
    assert m[idx[27].date()] is True          # day 27 drop known only on 28


def test_stats_and_bar():
    tr = [{"r": 2.0, "risk_pct": 1.0}, {"r": -1.0, "risk_pct": 3.0},
          {"r": -1.0, "risk_pct": 2.0}]
    row = g.s44_stats(tr, weeks=3)
    assert row[0] == 3 and row[1] == "1.00" and row[3] == "+0.000"
    assert row[4] == 1.0 and row[5] == 2.0 and row[6] == "2.00%"
    assert g.s44_verdict(tr)[0] == "not proposed"
    good = [{"r": 2.0, "risk_pct": 1}] * 20 + [{"r": -1.0, "risk_pct": 1}] * 30
    assert g.s44_verdict(good) == ("proposed for probation (max 1 position)", True)
    assert g.s44_verdict(good[:49])[1] is False


def test_replay_one_trade_per_symbol_day_and_explain(monkeypatch):
    bars = session(overrides={**OR, "09:50": (101, 103, 101, 102.5),
                              "09:55": (103, 103.5, 102.8, 103.2),
                              "10:30": (103.2, 112, 103, 111)})
    d = date(2026, 7, 15)
    frames = {"AAA": (bars, None)}
    tr = g.s44_replay(frames, {("AAA", d)}, 2.0, spy={d: True},
                      gaps={("AAA", d): 0.05})
    assert len(tr) == 1 and tr[0]["spy"] is True and tr[0]["gap"] == 0.05


def test_s44_news_fetch_uses_the_fake_client(tmp_path):
    calls = []

    class Fake:
        def company_news(self, sym, _from, to):
            calls.append((sym, _from, to))
            return [{"id": 1, "datetime": int(pd.Timestamp("2026-07-15 12:00",
                                                           tz="UTC").timestamp()),
                     "headline": "x"}]

    lim = g.S36Limiter(per_min=6000, sleep=lambda s: None)
    arts, bad = g.s36_news("AAA", [date(2026, 7, 15)], Fake(), lim, str(tmp_path))
    assert not bad and g.s36_has_news(arts, date(2026, 7, 15))
    g.s36_news("AAA", [date(2026, 7, 15)], Fake(), lim, str(tmp_path))
    assert len(calls) == 1                                # cached
