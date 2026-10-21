"""S36 - catalyst names vs the rest: gap, ADV, the 18 h news window (DST),
the 15-a-day cap, the split and the Finnhub fetch/cache. Synthetic data only,
no network (the Finnhub client is a fake)."""

import json
from datetime import date

import pandas as pd
import pytest

import study_exit_grid as g


def daily(rows, start="2026-03-02"):
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame([{"open": o, "high": max(o, c), "low": min(o, c),
                          "close": c, "volume": v} for o, c, v in rows], index=idx)


def test_gap_is_open_vs_previous_close():
    d = daily([(10, 10, 1), (10.4, 10.5, 1), (10.0, 10.0, 1)])
    gp = g.s36_gaps(d)
    assert pd.isna(gp.iloc[0])
    assert gp.iloc[1] == pytest.approx(0.04)
    assert gp.iloc[2] == pytest.approx(10.0 / 10.5 - 1)


def test_adv_uses_only_the_prior_sessions():
    d = daily([(10, 10, 100)] * 3 + [(10, 10, 1_000_000)])
    a = g.s36_adv(d, n=3)
    assert pd.isna(a.iloc[2])
    assert a.iloc[3] == pytest.approx(1000.0)        # today's huge volume excluded


def test_open_utc_is_dst_correct():
    assert g.s36_open_utc(date(2026, 1, 15)) == pd.Timestamp("2026-01-15 14:30", tz="UTC")
    assert g.s36_open_utc(date(2026, 7, 15)) == pd.Timestamp("2026-07-15 13:30", tz="UTC")
    # 2026-03-09 is the first session after the US spring-forward (03-08).
    assert g.s36_open_utc(date(2026, 3, 9)) == pd.Timestamp("2026-03-09 13:30", tz="UTC")


def ts(s):
    return int(pd.Timestamp(s, tz="UTC").timestamp())


def test_news_window_is_the_18_hours_before_the_open():
    day = date(2026, 7, 15)                          # open 13:30 UTC
    assert g.s36_has_news([{"datetime": ts("2026-07-14 19:30")}], day)
    assert not g.s36_has_news([{"datetime": ts("2026-07-14 19:29")}], day)
    assert g.s36_has_news([{"datetime": ts("2026-07-15 13:29")}], day)
    assert not g.s36_has_news([{"datetime": ts("2026-07-15 13:30")}], day)
    assert not g.s36_has_news([], day) and not g.s36_has_news(None, day)


def test_news_window_in_winter_shifts_an_hour():
    day = date(2026, 1, 15)                          # open 14:30 UTC
    assert g.s36_has_news([{"datetime": ts("2026-01-15 14:00")}], day)
    assert not g.s36_has_news([{"datetime": ts("2026-01-14 20:29")}], day)
    assert g.s36_has_news([{"datetime": ts("2026-01-14 20:30")}], day)


def frames_with(gap_open, close=100.0, vol=1_000_000, n=22):
    rows = [(close, close, vol)] * n + [(gap_open, gap_open, vol)]
    return (None, daily(rows, start="2026-01-01"))


def test_candidates_apply_gap_price_and_adv():
    frames = {"UP": frames_with(104.0), "DOWN": frames_with(96.0),
              "SMALL": frames_with(102.0), "THIN": frames_with(104.0, vol=1000),
              "PENNY": (None, daily([(4.0, 4.0, 10_000_000)] * 22
                                    + [(4.2, 4.2, 10_000_000)], start="2026-01-01"))}
    c = g.s36_candidates(frames, start="2026-01-01")
    names = sorted(s for v in c.values() for s, _ in v)
    assert names == ["DOWN", "UP"]


def test_cap_keeps_the_15_largest_gaps_among_kept_names():
    d = date(2026, 3, 2)
    cands = {d: [(f"S{i:02d}", 0.03 + i / 100) for i in range(20)]}
    top = g.s36_cap(cands)
    assert len(top) == 15 and ("S19", d) in top and ("S04", d) not in top
    # Filter first, then cap: dropping the 10 biggest lets small ones in.
    kept = g.s36_cap(cands, keep=lambda s, _d: int(s[1:]) < 10)
    assert kept == {(f"S{i:02d}", d) for i in range(10)}
    assert len(g.s36_cap({d: [("A", -0.09), ("B", 0.05)]}, n=1)) == 1
    assert g.s36_cap({d: [("A", -0.09), ("B", 0.05)]}, n=1) == {("A", d)}


def test_split_by_symbol_and_entry_day():
    t = [{"sym": "A", "day": date(2026, 3, 2), "r": 1},
         {"sym": "A", "day": date(2026, 3, 3), "r": 2},
         {"sym": "B", "day": date(2026, 3, 2), "r": 3}]
    cat, rest = g.s36_split(t, {("A", date(2026, 3, 2))})
    assert [x["r"] for x in cat] == [1] and [x["r"] for x in rest] == [2, 3]


def test_windows_merge_neighbours_and_respect_the_chunk():
    w = g.s36_windows([date(2026, 3, 2), date(2026, 3, 4), date(2026, 3, 20)])
    assert w == [(date(2026, 3, 1), date(2026, 3, 5)),
                 (date(2026, 3, 19), date(2026, 3, 21))]
    days = [date(2026, 1, 1) + pd.Timedelta(days=i).to_pytimedelta() for i in range(90)]
    for a, b in g.s36_windows(days, chunk=31):
        assert (b - a).days < 31


class FakeClient:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def company_news(self, symbol, _from, to):
        self.calls.append((symbol, _from, to))
        if symbol in self.fail:
            raise RuntimeError("FinnhubAPIException(status_code: 403)")
        return [{"id": 1, "datetime": ts(f"{_from} 20:00"), "headline": "h"}]


def no_wait():
    return g.S36Limiter(sleep=lambda s: None, clock=lambda: 0.0)


def test_news_is_cached_so_reruns_are_free(tmp_path):
    c = FakeClient()
    arts, bad = g.s36_news("AAA", [date(2026, 3, 2)], c, no_wait(), str(tmp_path))
    assert not bad and len(arts) == 1 and c.calls == [("AAA", "2026-03-01", "2026-03-03")]
    raw = json.loads((tmp_path / "AAA.json").read_text())
    assert list(raw["windows"]) == ["2026-03-01|2026-03-03"]
    arts2, _ = g.s36_news("AAA", [date(2026, 3, 2)], c, no_wait(), str(tmp_path))
    assert arts2 == arts and len(c.calls) == 1


def test_a_failed_symbol_is_flagged_and_not_cached(tmp_path):
    c = FakeClient(fail={"BAD"})
    arts, bad = g.s36_news("BAD", [date(2026, 3, 2)], c, no_wait(), str(tmp_path))
    assert bad and arts == [] and not (tmp_path / "BAD.json").exists()


def test_rate_limit_retries_once_after_sleeping(tmp_path):
    slept = []

    class Flaky(FakeClient):
        def company_news(self, symbol, _from, to):
            self.calls.append(symbol)
            if len(self.calls) == 1:
                raise RuntimeError("429 API limit reached")
            return []
    lim = g.S36Limiter(sleep=slept.append, clock=lambda: 0.0)
    c = Flaky()
    arts, bad = g.s36_news("X", [date(2026, 3, 2)], c, lim, cache_dir=str(tmp_path))
    assert not bad and len(c.calls) == 2 and 61 in slept


def test_limiter_spaces_calls_under_55_a_minute():
    now, slept = [0.0], []
    lim = g.S36Limiter(per_min=55, sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)),
                       clock=lambda: now[0])
    for _ in range(3):
        lim.wait()
    assert slept and all(s == pytest.approx(60 / 55) for s in slept)


def test_classify_end_to_end_with_a_fake_client(tmp_path):
    frames = {"NEWS": frames_with(104.0), "QUIET": frames_with(105.0),
              "BAD": frames_with(106.0)}

    class C(FakeClient):
        def company_news(self, symbol, _from, to):
            self.calls.append(symbol)
            if symbol == "BAD":
                raise RuntimeError("boom")
            if symbol == "QUIET":
                return []
            return [{"id": 9, "datetime": ts(f"{to} 00:00") - 86400 + 3600,
                     "headline": "x"}]
    info = g.s36_classify(frames, C(), no_wait(), start="2026-01-01",
                          cache_dir=str(tmp_path), log=None)
    day = frames["NEWS"][1].index[-1].date()
    assert info["news"] == {("NEWS", day)}
    assert info["gap"] == {("NEWS", day), ("QUIET", day), ("BAD", day)}
    assert info["failed"] == ["BAD"] and info["symbols"] == 3


def test_render_reports_both_splits():
    d = date(2026, 3, 2)
    trades = [{"setup": "momentum_continuation", "sym": "A", "day": d, "r": 2.0,
               "mfe_r": 2.0, "why": "target"},
              {"setup": "mean_reversion_reclaim", "sym": "B", "day": d, "r": -1.0,
               "mfe_r": 0.2, "why": "stop"}]
    info = {"news": {("A", d)}, "gap": {("A", d), ("B", d)}, "gap_days": 2,
            "gap_with_news": 1, "symbols": 2, "failed": ["Z"], "articles": 3,
            "first_article": d}
    text = "\n".join(g.s36_render(trades, info, 2, 2))
    assert "| gap + news (scanner rule) | all | catalyst | 1 | 100.0% | +2.000 |" in text
    assert "| gap only (no news test) | all | rest | 0 |" in text
    assert "1 symbol(s) failed" in text and "(Z)" in text
    assert "1 of them produced a trade" in text


def test_a_capped_window_that_misses_the_pre_open_is_refetched_narrow(tmp_path):
    day = date(2026, 7, 15)                          # window 07-14 19:30..07-15 13:30 UTC

    class Capped(FakeClient):
        def company_news(self, symbol, _from, to):
            self.calls.append((_from, to))
            if (_from, to) == ("2026-07-14", "2026-07-16"):
                # 250 articles, all after the open: newest-first truncation.
                return [{"id": i, "datetime": ts("2026-07-15 15:00") + i,
                         "headline": str(i)} for i in range(250)]
            return [{"id": 999, "datetime": ts("2026-07-15 12:00"), "headline": "pre"}]
    c = Capped()
    arts, bad = g.s36_news("MEGA", [day], c, no_wait(), str(tmp_path))
    assert c.calls == [("2026-07-14", "2026-07-16"), ("2026-07-14", "2026-07-15")]
    assert not bad and g.s36_has_news(arts, day)


def test_an_uncapped_empty_window_is_not_refetched():
    windows = {"2026-07-14|2026-07-16": [{"id": 1, "datetime": ts("2026-07-15 15:00")}]}
    assert not g.s36_needs_narrow(windows, date(2026, 7, 15))
