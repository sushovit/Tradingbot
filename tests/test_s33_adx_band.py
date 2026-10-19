"""S33 item 1 - ADX threshold study: rules, exit simulation, recommendation
gate. Synthetic bars only; no network."""

import pandas as pd
import pytest

import study_adx_band as s

ET = "America/New_York"


def bars(prices, start="2026-03-02 09:30"):
    idx = pd.date_range(start, periods=len(prices), freq="5min", tz=ET)
    rows = []
    for o, h, l, c in prices:
        rows.append({"open": o, "high": h, "low": l, "close": c, "volume": 1000})
    df = pd.DataFrame(rows, index=idx)
    df["atr"] = 1.0
    return df


def test_thresholds_current_minus_5_minus_10_and_regime():
    rules = dict(s.threshold_rules(30.0))
    assert list(rules) == ["current (30)", "-5 (25)", "-10 (20)", "regime (SPY ADX)"]
    assert rules["current (30)"](None) == 30 and rules["-10 (20)"](40) == 20
    reg = rules["regime (SPY ADX)"]
    assert (reg(30), reg(22), reg(15), reg(None)) == (20, 25, 30, 30)


def test_settings_follow_the_live_config():
    st = s.s33_settings()
    assert st["adx"] == 30 and st["atr_mult"] == 2.0 and st["rr"] == 3.0
    assert st["trail_atr"] == 2.5 and st["rsi"] == 55 and st["cap_atr"] == 3.0


def test_initial_stop_stands_until_plus_1r():
    # entry 100, stop 98 (R=2), target 106. Dips to 98.5 then to stop.
    df = bars([(100, 100, 100, 100), (100, 100.5, 98.5, 99), (99, 99, 97.5, 98)])
    assert s.simulate_intraday(df, 0, 100, 98, 106, 2.5) == (98, "stop", 2)


def test_after_plus_1r_the_trail_is_floored_at_entry():
    # close 102.2 (+1.1R) -> trail cand max(102.2-2.5, 100) = 100 (breakeven)
    df = bars([(100, 100, 100, 100), (100, 102.5, 100, 102.2),
               (102, 102, 99.5, 99.8)])
    px, why, n = s.simulate_intraday(df, 0, 100, 98, 106, 2.5)
    assert (px, why, n) == (100, "stop", 2)


def test_target_and_gap_fills():
    df = bars([(100, 100, 100, 100), (100, 106.5, 100, 106)])
    assert s.simulate_intraday(df, 0, 100, 98, 106, 2.5) == (106, "target", 1)
    df = bars([(100, 100, 100, 100), (97, 97, 96, 96.5)])
    assert s.simulate_intraday(df, 0, 100, 98, 106, 2.5) == (97, "stop (gap)", 1)
    df = bars([(100, 100, 100, 100), (100, 101, 99, 100)])
    assert s.simulate_intraday(df, 0, 100, 98, 106, 2.5) is None


def test_entry_windows_are_the_primary_hours():
    ts = lambda t: pd.Timestamp(f"2026-03-02 {t}", tz=ET)
    assert s.in_entry_window(ts("09:35")) and s.in_entry_window(ts("14:30"))
    assert not s.in_entry_window(ts("12:00")) and not s.in_entry_window(ts("15:00"))


def row(rule, subset, trades, exp, total):
    return {"rule": rule, "subset": subset, "trades": trades, "per_week": 1,
            "win": 50.0, "exp": exp, "total": total, "maxdd": 1.0}


def test_recommends_only_at_015r_with_more_trades():
    rows = [row("current (30)", "all admitted", 100, 0.20, 20),
            row("-5 (25)", "all admitted", 150, 0.16, 24),
            row("-10 (20)", "all admitted", 220, 0.12, 26),
            row("regime (SPY ADX)", "all admitted", 90, 0.30, 27)]
    best, cur = s.s33_recommend(rows)
    assert best["rule"] == "-5 (25)" and cur["trades"] == 100
    rows[1]["exp"] = 0.149
    assert s.s33_recommend(rows)[0] is None
    assert "No threshold recommended" in "\n".join(s.s33_render(rows, 3, 52, []))


def test_replay_admits_lower_adx_only_at_lower_thresholds(monkeypatch):
    """Two identical winning signals, ADX 32 and ADX 24: current takes one,
    -10 takes both, and the second is flagged as live-blocked."""
    df = bars([(100, 100, 100, 100)] * 4 + [(100, 106.5, 100, 106)] * 2)
    sigs = {"AAA": {"adx": 32.0}, "BBB": {"adx": 24.0}}

    def fake_signals(d, sym, st):
        return [{"symbol": sym, "i": 3, "ts": d.index[3] + pd.Timedelta(minutes=5),
                 "adx": sigs[sym]["adx"], "atr": 1.0, "entry": 100.0}]
    monkeypatch.setattr(s, "candidate_signals", fake_signals)
    monkeypatch.setattr(s, "add_indicators", lambda d: d)
    monkeypatch.setattr(s, "spy_regime_maps", lambda spy: (
        {d.date(): True for d in df.index}, {d.date(): 30.0 for d in df.index}))
    monkeypatch.setattr(s, "_daily_atr", lambda raw: ([], []))
    st = dict(s.s33_settings(), atr_mult=2.0)
    res = s.s33_replay({"AAA": df, "BBB": df}, df, st,
                       start="2026-03-01", end="2026-03-03")
    assert len(res["current (30)"]) == 1 and len(res["-10 (20)"]) == 2
    assert len(res["-5 (25)"]) == 1 and len(res["regime (SPY ADX)"]) == 2
    blocked = [t for t in res["-10 (20)"] if t["blocked_live"]]
    assert [t["symbol"] for t in blocked] == ["BBB"] and blocked[0]["r"] == pytest.approx(3.0)
    rows = s.s33_rows(res, 1.0)
    assert {r["rule"] for r in rows if r["subset"] == "adx_low only"} == {
        "-5 (25)", "-10 (20)", "regime (SPY ADX)"}


def test_the_study_never_touches_the_live_config():
    src = open("study_adx_band.py", encoding="utf-8").read()
    assert "json.dump" not in src and 'open("bot_config.json", "w"' not in src
