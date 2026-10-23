"""S50 (CEO 2026-10-23): initial stop re-anchored to the broker fill (same
1.5-2.5 ATR clamp, from the FILL; same share count) vs the current stop from
the signal price."""

import position_mgmt as pm
import study_exit_grid as g

P = dict(pm.R_ATR_DEFAULTS)


def test_levels_same_shares_stop_moves_with_the_fill():
    # KMI-like: fill 34.01, structural 33.40, ATR 0.60, signal 0.83% higher
    lv = g.s50_levels(34.01, 33.40, 0.60, 0.0083, P, 3.0)
    sig = 34.01 / (1 - 0.0083)
    s_cur, t_cur = lv["current"]
    s_anc, t_anc = lv["anchored"]
    assert s_cur == pm.initial_stop(sig, 33.40, 0.60, P)
    assert s_anc == pm.initial_stop(34.01, 33.40, 0.60, P)
    assert s_anc < s_cur                      # the clamp from a lower fill
    assert 34.01 - s_anc >= 1.5 * 0.60 - 0.011  # >= 1.5 ATR below the fill
    assert t_anc == pm.capped_target(34.01, s_anc, 0.60, 3.0, 3.0)
    assert lv["shares"] >= 1


def test_no_gap_means_the_two_stops_are_identical():
    lv = g.s50_levels(100.0, 96.0, 2.0, 0.0, P, 3.0)
    assert lv["current"][0] == lv["anchored"][0]


def test_stats_and_rule():
    t = lambda pnl, risk, d: {"pnl": pnl, "risk_usd": risk, "when": d}
    cur = g.s50_stats([t(10, 40, 1), t(-40, 40, 2)])
    anc = g.s50_stats([t(15, 48, 1), t(-48, 48, 2)])
    assert cur["exp_usd"] == -15 and anc["avg_risk"] == 48
    assert g.s50_decision(cur, anc) is False                 # lower expectancy
    better = g.s50_stats([t(30, 49, 1), t(-20, 49, 2)])
    assert g.s50_decision(cur, better) is True
    too_big = g.s50_stats([t(30, 60, 1), t(-20, 60, 2)])
    assert g.s50_decision(cur, too_big) is False             # avg risk > $50
    assert better["maxdd_usd"] == 20 and too_big["over_budget"] == 100.0
