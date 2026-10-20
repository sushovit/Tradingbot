"""S34 - target grid: half-at-+1R arithmetic, the variant list and the adopt
rule. Synthetic bars only."""

import pandas as pd

import study_exit_grid as g

ET = "America/New_York"
STEPS = [(1.0, 0.0), (1.5, 0.0), (2.0, 1.0)]


def m5(prices, day="2026-03-02", start="09:30"):
    idx = pd.date_range(f"{day} {start}", periods=len(prices), freq="5min", tz=ET)
    return pd.DataFrame([{"open": o, "high": h, "low": low, "close": c, "volume": 100}
                         for o, h, low, c in prices], index=idx)


DAY = pd.Timestamp("2026-03-02", tz=ET)


def sim(bars, target, half):
    return g.simulate_half(bars, {}, {}, DAY, 100.0, 98.0, target, STEPS, half)


def test_variants_are_the_eight_cells():
    labels = [v[0] for v in g.s34_variants()]
    assert labels == ["1.5R", "1.5R + half@1R", "2R", "2R + half@1R",
                      "2.5R", "2.5R + half@1R", "3R", "3R + half@1R"]
    assert g.s34_label(*g.S34_CURRENT) == "3R"


def test_full_position_to_target():
    bars = m5([(100, 100, 100, 100), (100, 104.5, 99.5, 104)])
    assert sim(bars, 104.0, False)[:2] == (2.0, "target")


def test_half_at_1r_then_target_averages_the_two_halves():
    bars = m5([(100, 100, 100, 100), (100, 104.5, 99.5, 104)])
    r, why, _ = sim(bars, 104.0, True)
    assert why == "target" and r == 0.5 * 1.0 + 0.5 * 2.0


def test_half_at_1r_then_back_to_breakeven():
    # 10:00+: close 102.5 (+1.25R) -> ladder stop to entry 100; then stopped.
    bars = m5([(100, 100, 100, 100), (100, 102.6, 100.5, 102.5),
               (102, 102, 99.5, 99.8)], start="09:55")
    r, why, mfe = sim(bars, 106.0, True)
    assert why == "stop" and r == 0.5 * 1.0 + 0.5 * 0.0 and abs(mfe - 1.3) < 1e-9
    r_full, _, _ = sim(bars, 106.0, False)
    assert r_full == 0.0


def test_a_stop_before_1r_loses_the_full_risk_either_way():
    bars = m5([(100, 100, 100, 100), (100, 101, 97.5, 98)])
    assert sim(bars, 106.0, True)[0] == -1.0 and sim(bars, 106.0, False)[0] == -1.0


def trades(n, r, mfe):
    return [{"setup": "x", "r": r, "mfe_r": mfe, "why": "target"} for _ in range(n)]


def test_adopt_rule_needs_expectancy_capture_and_200_trades():
    res = {label: trades(250, 0.30, 1.5) for label, *_ in g.s34_variants()}
    res["3R"] = trades(250, 0.30, 1.5)                     # capture 20%
    res["2R"] = trades(250, 0.30, 1.0)                     # same exp, capture 30%
    assert g.s34_adopt(res)[0] == "2R"
    res["2R"] = trades(150, 0.40, 1.0)                     # too few trades
    assert g.s34_adopt(res)[0] is None
    res["2R"] = trades(250, 0.29, 0.5)                     # better capture, lower exp
    assert g.s34_adopt(res)[0] is None


def test_daily_bars_are_resampled_from_rth_5_minute_bars():
    bars = m5([(10, 11, 9, 10.5), (10.5, 12, 10, 11)])
    d = g.daily_from_m5(bars)
    assert len(d) == 1 and (d.iloc[0]["open"], d.iloc[0]["high"],
                            d.iloc[0]["low"], d.iloc[0]["close"]) == (10, 12, 9, 11)
