"""S35 (CEO 2026-10-20) - the backtest must hand every setup at least the
bars its detector needs. It handed pullback_in_uptrend 60 of 62, so the setup
never fired in any backtest."""

import pandas as pd

import backtest
from strategies import REGISTRY


def test_every_setup_has_a_declared_minimum():
    assert set(backtest.setup_min_bars()) == set(REGISTRY)


def test_the_window_covers_every_setups_minimum():
    mins = backtest.setup_min_bars()
    assert backtest.DETECT_WINDOW >= 70
    for name, need in mins.items():
        assert backtest.DETECT_WINDOW >= need, (name, need)
    assert mins["pullback_in_uptrend"] == 62


def test_collect_signals_hands_the_detector_the_full_window(monkeypatch):
    seen = []

    class Probe:
        def detect(self, window, context):
            seen.append(len(window))
            return None
    monkeypatch.setitem(backtest.REGISTRY, "probe", Probe)
    idx = pd.date_range("2025-01-01", periods=150, freq="B")
    df = pd.DataFrame({"open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0,
                       "volume": 1000.0}, index=idx)
    backtest.collect_signals("X", df, "probe", None, {}, {})
    assert max(seen) == backtest.DETECT_WINDOW
    assert seen[-1] >= backtest.setup_min_bars()["pullback_in_uptrend"]


def test_a_real_pullback_setup_is_no_longer_starved():
    """Below 62 bars the live detector returns None before looking."""
    det = REGISTRY["pullback_in_uptrend"]()
    idx = pd.date_range("2025-01-01", periods=61, freq="B")
    df = pd.DataFrame({"open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0,
                       "volume": 1000.0}, index=idx)
    assert det.detect(df, {"ticker": "X", "risk_profile": {}, "config": {}}) is None


# ---------------------------------------------------------- probation (S35)
import json  # noqa: E402

import risk  # noqa: E402


def shipped():
    return json.load(open("bot_config.json", encoding="utf-8"))


def test_pullback_is_retired_not_on_probation():
    """CEO 2026-10-21: retired - off the live list and the probation list."""
    cfg = shipped()
    assert "pullback_in_uptrend" not in risk.probation_setups(cfg)
    assert "pullback_in_uptrend" not in cfg["default_strategies"]
    assert risk.probation_max_concurrent(cfg) == 2          # agenda 14 unchanged


def test_a_per_setup_slot_override_still_works():
    """The S35 mechanism stays for any future setup."""
    cfg = {"setup_probation": {"setups": ["x_setup"], "max_concurrent": 2,
                               "max_concurrent_by_setup": {"x_setup": 1}}}
    assert risk.check_setup_probation("x_setup", 0, 0, cfg) == (True, None)
    assert risk.check_setup_probation("x_setup", 1, 0, cfg) == (
        False, "probation_position_open")
