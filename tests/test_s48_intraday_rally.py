"""S48 (CEO P4) - gap-free intraday rallies: gap/news filter, trigger with the
sector-ETF check, stop/entry/target, forced 15:55 exit, max-1-open, ETF map.
Synthetic data only, no network (the Finnhub client is a fake)."""

from datetime import date

import pandas as pd
import pytest

import study_exit_grid as g

DAY = "2026-07-15"
D = date(2026, 7, 15)


def session(day=DAY, overrides=None, base=100.0):
    """78 flat 5-minute RTH bars at `base`; overrides {"HH:MM": (o, h, l, c)}."""
    idx = pd.date_range(f"{day} 09:30", periods=78, freq="5min", tz=g.ET)
    df = pd.DataFrame([(base, base, base, base)] * 78, index=idx,
                      columns=["open", "high", "low", "close"])
    df["volume"] = 1000
    for hm, (o, h, l, c) in (overrides or {}).items():
        df.loc[pd.Timestamp(f"{day} {hm}", tz=g.ET), ["open", "high", "low", "close"]] = (o, h, l, c)
    return df


ETF_UP = session(base=51.0)        # prior close 50 -> up all day
ETF_DOWN = session(base=49.0)


def hm(ts):
    return ts.strftime("%H:%M")


# ---------------------------------------------------------------- filters

def test_gap_free_and_news_thresholds():
    assert g.s48_gap_free(0.029) and g.s48_gap_free(-0.029)
    assert not g.s48_gap_free(0.03) and not g.s48_gap_free(-0.031)
    assert not g.s48_gap_free(float("nan"))
    assert not g.s48_needs_news(0.0149) and g.s48_needs_news(0.015)
    assert g.s48_needs_news(-0.02)


def test_news_filter_drops_only_big_gap_days_with_news():
    t = [{"sym": "A", "day": D, "gap": 0.02}, {"sym": "B", "day": D, "gap": 0.01},
         {"sym": "C", "day": D, "gap": 0.02}]
    kept = g.s48_news_filter(t, {("A", D), ("B", D)})
    assert [x["sym"] for x in kept] == ["B", "C"]


def test_etf_map_follows_sector_tags():
    assert g.s48_etf_for("DDOG") == "XLK" and g.s48_etf_for("NVDA") == "SMH"
    assert g.s48_etf_for("JPM") == "XLF" and g.s48_etf_for("XOM") == "XLE"
    assert g.s48_etf_for("NFLX") == "XLC" and g.s48_etf_for("TSLA") == "XLY"
    assert g.s48_etf_for("NEE") == "XLU" and g.s48_etf_for("PLD") == "XLRE"
    assert g.s48_etf_for("MSTR") is None          # crypto_dat: excluded
    assert g.s48_etf_for("ZZZZ") is None          # unclassified: excluded


# ---------------------------------------------------------------- trigger

def test_trigger_first_close_at_plus_3pct_with_etf_up():
    bars = session(overrides={"09:50": (101, 103.5, 101, 102.9),
                              "10:05": (102.9, 104, 102.5, 103.0)})
    i, st = g.s48_trigger(bars, 100.0, ETF_UP, 50.0)
    assert st == "ok" and hm(bars.index[i]) == "10:05"


def test_trigger_window_ends_with_the_1125_bar():
    bars = session(overrides={"11:25": (101, 103.5, 101, 103.0)})
    i, st = g.s48_trigger(bars, 100.0, ETF_UP, 50.0)
    assert hm(bars.index[i]) == "11:25"
    bars = session(overrides={"11:30": (101, 103.5, 101, 103.0)})
    assert g.s48_trigger(bars, 100.0, ETF_UP, 50.0) == (None, "no_trigger")


def test_etf_down_at_the_first_trigger_means_no_trade():
    bars = session(overrides={"09:45": (101, 104, 101, 103.2)})
    i, st = g.s48_trigger(bars, 100.0, ETF_DOWN, 50.0)
    assert st == "etf_down" and hm(bars.index[i]) == "09:45"
    assert g.s48_trade(bars, 100.0, ETF_DOWN, 50.0)["status"] == "etf_down"
    assert g.s48_trade(bars, 100.0, None, None)["status"] == "etf_down"


def test_both_variant_waits_for_the_etf():
    etf = session(base=49.0, overrides={"10:00": (49, 51, 49, 50.5)})
    etf.loc[etf.index > pd.Timestamp(f"{DAY} 10:00", tz=g.ET), "close"] = 50.5
    bars = session(overrides={"09:45": (101, 104, 101, 103.2),
                              "10:00": (103, 104, 102, 103.4)})
    i, st = g.s48_trigger(bars, 100.0, etf, 50.0, both=True)
    assert st == "ok" and hm(bars.index[i]) == "10:00"


def test_etf_bar_missing_uses_the_last_one_before():
    etf = ETF_UP.drop(pd.Timestamp(f"{DAY} 09:45", tz=g.ET))
    assert g.s48_etf_up(etf, 50.0, pd.Timestamp(f"{DAY} 09:45", tz=g.ET))
    assert not g.s48_etf_up(etf, 50.0, pd.Timestamp(f"{DAY} 09:25", tz=g.ET))


# ---------------------------------------------------------------- trade

def test_trade_entry_next_open_stop_day_low_target_2r():
    bars = session(overrides={"09:30": (100, 100.5, 98, 100),     # day low 98
                              "09:45": (101, 103.5, 100.5, 103.0),
                              "09:50": (103, 103.5, 102.8, 103.2),
                              "10:30": (103, 113.5, 103, 112)})
    t = g.s48_trade(bars, 100.0, ETF_UP, 50.0, 2.0)
    assert t["status"] == "traded"
    assert t["entry"] == 103 and t["stop"] == 98 and t["target"] == 113
    assert t["r"] == pytest.approx(2.0) and t["why"] == "target"
    assert hm(t["t_trig"]) == "09:45" and hm(t["t_entry"]) == "09:50"
    assert hm(t["t_exit"]) == "10:30"
    assert t["risk_pct"] == pytest.approx(500 / 103)


def test_stop_low_includes_the_trigger_bar_only_up_to_it():
    bars = session(overrides={"09:45": (101, 103.5, 97.0, 103.0),
                              "09:50": (103, 103.5, 90.0, 103.2)})
    t = g.s48_trade(bars, 100.0, ETF_UP, 50.0)
    assert t["stop"] == 97.0 and t["why"] == "stop" and t["r"] == pytest.approx(-1.0)


def test_bar_spanning_stop_and_target_takes_the_stop():
    bars = session(overrides={"09:30": (100, 100, 99, 100),
                              "09:45": (101, 103.5, 100.5, 103.0),
                              "09:50": (103, 103, 103, 103),
                              "10:00": (103, 120, 90, 103)})
    t = g.s48_trade(bars, 100.0, ETF_UP, 50.0)
    assert t["why"] == "stop" and t["r"] == pytest.approx(-1.0)


def test_gap_through_levels_fills_at_open():
    over = {"09:30": (100, 100, 99, 100), "09:45": (101, 103.5, 100.5, 103.0),
            "09:50": (103, 103, 103, 103)}
    t = g.s48_trade(session(overrides={**over, "10:00": (95, 96, 94, 95)}), 100.0, ETF_UP, 50.0)
    assert t["why"] == "stop (gap)" and t["r"] == pytest.approx(-2.0)
    t = g.s48_trade(session(overrides={**over, "10:00": (115, 116, 114, 115)}), 100.0, ETF_UP, 50.0)
    assert t["why"] == "target (gap)" and t["r"] == pytest.approx(3.0)


def test_forced_exit_at_the_1555_open():
    bars = session(overrides={"09:30": (100, 100, 99, 100),
                              "09:45": (101, 103.5, 100.5, 103.0),
                              "09:50": (103, 103, 103, 103)})
    bars.loc[bars.index >= pd.Timestamp(f"{DAY} 09:55", tz=g.ET),
             ["open", "high", "low", "close"]] = 104.0
    t = g.s48_trade(bars, 100.0, ETF_UP, 50.0)
    assert t["why"] == "time 15:55" and hm(t["t_exit"]) == "15:55"
    assert t["r"] == pytest.approx(0.25)


def test_no_risk_is_skipped():
    # 09:30 closes +4% (trigger), next bar opens below its low: risk <= 0
    bars2 = session(base=104.0)
    bars2.loc[bars2.index[1], "open"] = 103.0      # opens below the 09:30 low
    t2 = g.s48_trade(bars2, 100.0, ETF_UP, 50.0)
    assert t2["status"] == "no_risk"


def test_max_open_one_across_symbols():
    ts = lambda s: pd.Timestamp(f"{DAY} {s}", tz=g.ET)
    tr = [{"sym": "A", "t_entry": ts("09:40"), "t_exit": ts("10:30"), "r": 1},
          {"sym": "B", "t_entry": ts("10:00"), "t_exit": ts("11:00"), "r": 1},
          {"sym": "C", "t_entry": ts("10:30"), "t_exit": ts("12:00"), "r": 1}]
    assert [t["sym"] for t in g.s48_max_open(tr, 1)] == ["A", "C"]
    assert len(g.s48_max_open(tr, 2)) == 3


# ---------------------------------------------------------------- scan

def frames_for(prev_bars, day_bars):
    return g.s48_sessions(pd.concat([prev_bars, day_bars]))


def test_scan_skips_gap_days_and_unmapped_symbols():
    prev = session("2026-07-14")                                  # close 100
    up = session(overrides={"09:45": (101, 103.5, 100.5, 103.0),
                            "09:50": (103, 103.5, 102.5, 103),
                            "10:30": (103, 120, 103, 119)})
    gapped = session(base=104.0)                                  # +4% gap
    etf = frames_for(session("2026-07-14", base=50.0), ETF_UP)
    frames = {"DDOG": frames_for(prev, up), "ZS": frames_for(prev, gapped),
              "MSTR": frames_for(prev, up)}
    runs, cnt, unmapped = g.s48_scan(frames, {"XLK": etf}, "2026-07-15",
                                     "2026-07-15", ks=(2.0,))
    assert unmapped == ["MSTR"]
    assert cnt["gap_excluded"] == 1 and cnt["gap_free"] == 1
    assert [t["sym"] for t in runs[2.0]] == ["DDOG"]
    assert runs[2.0][0]["etf"] == "XLK" and runs[2.0][0]["gap"] == pytest.approx(0.0)


# ---------------------------------------------------------------- news

class FakeFinnhub:
    def __init__(self, articles=None, fail=()):
        self.articles, self.fail, self.calls = articles or {}, set(fail), []

    def company_news(self, sym, _from, to):
        self.calls.append((sym, _from, to))
        if sym in self.fail:
            raise RuntimeError("boom")
        return self.articles.get(sym, [])


class NoWait:
    def wait(self):
        pass

    def sleep(self, s):
        pass


def test_news_fetch_with_fake_client(tmp_path):
    lo, hi = g.s36_news_window(D)
    fake = FakeFinnhub({"AAA": [{"id": 1, "datetime": hi - 60, "headline": "x"}],
                        "BBB": [{"id": 2, "datetime": lo - 60, "headline": "y"}]},
                       fail={"CCC"})
    has, failed, n, first = g.s48_fetch_news(
        {("AAA", D), ("BBB", D), ("CCC", D)}, fake, NoWait(), str(tmp_path))
    assert has == {("AAA", D)}
    assert failed == ["CCC"]            # failure counts as no news
    assert n == 2 and fake.calls


def test_verdict_bar():
    assert g.s48_verdict([{"r": 0.2}] * 50)[1]
    assert not g.s48_verdict([{"r": 0.2}] * 49)[1]
    assert not g.s48_verdict([{"r": 0.1}] * 80)[1]
