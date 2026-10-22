"""S45 - funnel replay per blocker (CEO 2026-10-22, item B). Synthetic bars
only; no network.

The heart of it: the study copies of the detectors must be the live
detectors with one change - they report EVERY failing filter instead of the
first. Proved here on synthetic data: same trigger, same first failure,
same entry/stop on a pass."""

import numpy as np
import pandas as pd
import pytest

import study_adx_band as ab
import study_exit_grid as g
from strategies.base import Rejection, Signal
from strategies.mean_reversion_reclaim import MeanReversionReclaim
from strategies.momentum_continuation import MomentumContinuation
from strategies.trend_continuation import TrendContinuation

ET = "America/New_York"
LIVE_CFG = {"volume_multipliers": {"momentum_continuation": 1.0,
                                   "mean_reversion_reclaim": 1.3},
            "use_rsi_filter": True, "rsi_threshold": 55,
            "use_confirmation_candle": True}
PROFILE = {"fast_ema": 9, "slow_ema": 21, "adx_threshold": 30, "rr_ratio": 3.0,
           "atr_multiplier": 2.0, "use_volume_filter": True}


def daily_bars(seed, n=260):
    """A choppy random walk with shocks: breakouts, washouts and reclaims."""
    rng = np.random.default_rng(seed)
    ret = rng.normal(0, 0.025, n) + rng.choice([0, 0.07, -0.08], n, p=[.9, .05, .05])
    close = 50 * np.exp(np.cumsum(ret))
    open_ = close * np.exp(rng.normal(0, 0.012, n))
    open_[1:] = np.where(rng.random(n - 1) < .5, close[:-1] * np.exp(rng.normal(0, .01, n - 1)), open_[1:])
    high = np.maximum(open_, close) * (1 + rng.uniform(0, .02, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, .02, n))
    vol = rng.integers(5e5, 3e6, n).astype(float)
    vol[rng.random(n) < .03] = 0.0
    idx = pd.bdate_range("2025-01-02", periods=n)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": vol}, index=idx)


@pytest.mark.parametrize("setup,cls", [("momentum_continuation", MomentumContinuation),
                                       ("mean_reversion_reclaim", MeanReversionReclaim)])
def test_daily_study_copy_matches_the_live_detector(setup, cls):
    live = cls()
    ctx = {"ticker": "X", "risk_profile": PROFILE, "config": LIVE_CFG}
    seen = {"signal": 0, "rejection": 0, "names": set()}
    for seed in range(6):
        df = daily_bars(seed)
        for i in range(30, len(df)):
            w = g.s45_entry_window(df, i)
            got = g.s45_daily_check(setup, w, g._vol_mult(setup, LIVE_CFG))
            want = live.detect(w, ctx)
            if want is None:
                assert got is None
                continue
            assert got is not None
            if isinstance(want, Signal):
                seen["signal"] += 1
                assert got["fails"] == []
                assert got["entry"] == pytest.approx(want.entry)
                assert got["stop"] == pytest.approx(want.stop)
            else:
                seen["rejection"] += 1
                seen["names"].add(want.filter_name)
                assert got["fails"] and got["fails"][0] == want.filter_name
    assert seen["signal"] > 0 and seen["rejection"] > 0
    assert "volume_low" in seen["names"]


def m5_bars(seed, n=900):
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    close = 100 + 4 * np.sin(t / 37.0) + np.cumsum(rng.normal(0, .25, n))
    open_ = np.r_[close[0], close[:-1]] + rng.normal(0, .05, n)
    high = np.maximum(open_, close) + rng.uniform(0, .3, n)
    low = np.minimum(open_, close) - rng.uniform(0, .3, n)
    vol = rng.integers(1000, 9000, n).astype(float)
    days = pd.bdate_range("2026-03-02", periods=n // 78 + 2)
    idx = [d + pd.Timedelta(hours=9, minutes=30 + 5 * m) for d in days for m in range(78)][:n]
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": vol}, index=pd.DatetimeIndex(idx).tz_localize(ET))


@pytest.mark.parametrize("adx,rsi", [(30.0, 55.0), (12.0, 45.0)])
def test_trend_study_copy_matches_the_live_detector(adx, rsi):
    # The live thresholds (30/55), and looser ones so the synthetic walk
    # also produces passes.
    live = TrendContinuation()
    ctx = {"ticker": "X", "risk_profile": dict(PROFILE, adx_threshold=adx),
           "config": dict(LIVE_CFG, rsi_threshold=rsi)}
    st = {"adx": adx, "rsi": rsi, "confirm": True, "volume": True}
    seen = {"signal": 0, "names": set()}
    for seed in range(4):
        raw = m5_bars(seed)
        df = ab.add_indicators(raw)
        for j in range(60, len(df) - 2):
            want = live.detect(raw.iloc[:j + 2], ctx)
            got = g.s45_trend_fails(df, j, st)
            if want is None:
                assert got is None
                continue
            assert got is not None
            if isinstance(want, Signal):
                seen["signal"] += 1
                assert got == []
                assert want.entry == pytest.approx(float(df["close"].iloc[j + 1]))
                assert want.stop == pytest.approx(want.entry - 2.0 * float(df["atr"].iloc[j]))
            else:
                assert isinstance(want, Rejection)
                seen["names"].add(want.filter_name)
                assert got and got[0] == want.filter_name
    assert seen["names"]
    if adx < 30:
        assert seen["signal"] > 0
    else:
        assert "adx_low" in seen["names"]


def test_trend_candidates_add_worker_gates_in_order():
    raw = m5_bars(1)
    df = ab.add_indicators(raw)
    st = {"adx": 0.0, "rsi": None, "confirm": False, "volume": False,
          "atr_mult": 2.0}
    bearish = {d: False for d in set(df.index.date)}
    cands = g.s45_trend_candidates(df, "X", st, bearish, start="2026-01-01",
                                   end="2026-12-31")
    assert cands
    for c in cands:
        assert c["fails"][0] == "spy_bearish"
        inside = ab.in_entry_window(df.index[c["i"]])
        assert ("outside_hours" in c["fails"]) == (not inside)


def test_blocked_means_the_only_failure():
    # Two trend candidates: one blocked by rsi_low only, one by rsi_low AND
    # outside_hours. Only the first counts against rsi_low.
    df = pd.DataFrame({"open": [100.0] * 6, "high": [100.0] * 6, "low": [100.0] * 6,
                       "close": [100.0] * 6, "atr": [1.0] * 6},
                      index=pd.date_range("2026-03-02 10:00", periods=6, freq="5min", tz=ET))
    df.iloc[2, :4] = [100, 104, 100, 103]     # rallies to... no target hit
    df.iloc[3, :4] = [103, 106.5, 103, 106]   # target 106 hit
    df.iloc[5, :4] = [100, 100, 100, 100]
    datr = {}
    mk = lambda i, fails: {"setup": g.S45_TREND, "sym": "X", "i": i, "ts": df.index[i],
                           "entry": 100.0, "atr": 1.0, "fails": fails}
    prepared = {"X": (df, datr, [mk(0, ["rsi_low"]), mk(4, ["rsi_low", "outside_hours"])])}
    st = {"atr_mult": 2.0, "rr": 3.0, "cap_atr": 3.0, "trail_atr": 2.5}
    out = g.s45_run([], {}, prepared, st)
    assert out["rsi_low"]["signals"] == 1
    assert [t["r"] for t in out["rsi_low"]["blocked"]] == [pytest.approx(3.0)]
    assert out["outside_hours"]["signals"] == 0 and out["_passed"] == []


def test_daily_outside_hours_does_not_apply():
    assert "momentum_continuation" not in g.S45_APPLIES["outside_hours"]
    assert "mean_reversion_reclaim" not in g.S45_APPLIES["spy_bearish"]


def test_ceo_rule_verbatim():
    st = lambda n, e: {"trades": n, "expectancy_r": e}
    assert g.s45_verdict(st(50, 0.15))
    assert not g.s45_verdict(st(49, 0.40))
    assert not g.s45_verdict(st(500, 0.149))
    assert not g.s45_verdict(st(0, None))


def test_render_states_the_verdict():
    trades = [{"setup": g.S45_TREND, "sym": "X", "i": n, "when": pd.Timestamp("2026-03-02", tz=ET)
               + pd.Timedelta(minutes=5 * n), "r": 1.0 if n % 2 else -0.5}
              for n in range(60)]
    res = {"_passed": trades}
    for x in g.S45_APPLIES:
        res[x] = {"blocked": trades if x == "rsi_low" else [], "signals": 60 if x == "rsi_low" else 0,
                  "passed": trades}
    text = "\n".join(g.s45_render(res, 3, 10, 60))
    assert "met by rsi_low (+0.250R over 60)" in text
    assert "S33 (item 20)" in text


@pytest.mark.parametrize("setup,cls,off", [("momentum_continuation", MomentumContinuation, -1.0),
                                           ("mean_reversion_reclaim", MeanReversionReclaim, 0.0)])
def test_filters_after_volume_are_still_evaluated(setup, cls, off):
    # Switch the live volume filter OFF through the config multiplier: the
    # live detector's verdict must then equal the study's failure list
    # with volume_low removed - i.e. the study really checks every filter
    # AFTER the one it reports first.
    live = cls()
    ctx = {"ticker": "X", "risk_profile": PROFILE,
           "config": {"volume_multipliers": {setup: off}}}
    checked = 0
    for seed in range(6):
        df = daily_bars(seed)
        for i in range(30, len(df)):
            w = g.s45_entry_window(df, i)
            if float(w["volume"].iloc[-22:-2].mean()) <= 0:
                continue
            got = g.s45_daily_check(setup, w, g._vol_mult(setup, LIVE_CFG))
            if got is None or "volume_low" not in got["fails"]:
                continue
            rest = [f for f in got["fails"] if f != "volume_low"]
            want = live.detect(w, ctx)
            if rest:
                assert isinstance(want, Rejection) and want.filter_name == rest[0]
            else:
                assert isinstance(want, Signal)
            checked += 1
    assert checked > 5
