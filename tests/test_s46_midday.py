"""S46 (CEO 2026-10-23): the midday re-run - entries 11:30-14:00 ET only,
forced exit at the 15:55 ET bar's open, never held overnight."""

import pandas as pd

import study_exit_grid as g

ET = "America/New_York"


def bars(start, prices, atr=1.0):
    idx = pd.date_range(start, periods=len(prices), freq="5min", tz=ET)
    df = pd.DataFrame([{"open": o, "high": h, "low": l, "close": c}
                       for o, h, l, c in prices], index=idx)
    df["atr"] = atr
    return df


def test_midday_window_bounds():
    ts = lambda t: pd.Timestamp(f"2026-03-02 {t}", tz=ET)
    assert g.in_midday(ts("11:30")) and g.in_midday(ts("13:55"))
    assert not g.in_midday(ts("11:25")) and not g.in_midday(ts("14:00"))


def test_flat_trade_is_closed_at_the_1555_open_same_day():
    # entry at 15:45 bar close, nothing hits -> exits at the 15:55 open
    df = bars("2026-03-02 15:45", [(100, 100, 100, 100), (100, 100.5, 99.8, 100.2),
                                   (100.3, 100.6, 100.1, 100.4)])
    px, why, n = g.simulate_intraday_eod(df, 0, 100.0, 98.0, 106.0, 2.5)
    assert (px, why, n) == (100.3, "time exit 15:55", 2)


def test_never_held_overnight():
    df = pd.concat([bars("2026-03-02 15:50", [(100, 100, 100, 100)]),
                    bars("2026-03-03 09:30", [(103, 104, 102, 103.5)])])
    px, why, n = g.simulate_intraday_eod(df, 0, 100.0, 98.0, 106.0, 2.5)
    assert why == "time exit 15:55" and n == 1 and px == 103   # next-day first bar, flagged


def test_stop_and_target_still_work_before_1555():
    df = bars("2026-03-02 12:00", [(100, 100, 100, 100), (100, 106.5, 99.5, 106)])
    assert g.simulate_intraday_eod(df, 0, 100.0, 98.0, 106.0, 2.5)[:2] == (106.0, "target")
    df = bars("2026-03-02 12:00", [(100, 100, 100, 100), (100, 100.2, 97.5, 98)])
    assert g.simulate_intraday_eod(df, 0, 100.0, 98.0, 106.0, 2.5)[:2] == (98.0, "stop")


def test_only_outside_hours_only_midday_signals_and_max_one_open():
    df = bars("2026-03-02 11:30", [(100, 100.2, 99.9, 100)] * 60)
    st = {"atr_mult": 2.0, "rr": 3.0, "trail_atr": 2.5, "cap_atr": 3.0}

    def c(i, fails):
        return {"setup": "trend_continuation", "sym": "A", "i": i,
                "ts": df.index[i] + pd.Timedelta(minutes=5), "entry": 100.0,
                "atr": 1.0, "fails": fails}
    cands = [c(0, ["outside_hours"]), c(2, ["outside_hours", "volume_low"]),
             c(40, ["outside_hours"])]                     # 40 -> 14:50: not midday
    prepared = {"A": (df, {}, cands), "B": (df, {}, [dict(cands[0], sym="B")])}
    all_t = g.s46_trades(prepared, st)
    assert sorted(t["sym"] for t in all_t) == ["A", "B"]   # one per symbol, midday only
    one = g.s46_trades(prepared, st, max_open=1)
    assert len(one) == 1                                   # B overlaps A -> skipped
