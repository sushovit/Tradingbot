"""
Volume-threshold calibration study. No network.
"""

import pandas as pd
import pytest

import volume_calibration as vc
from strategies import momentum_continuation as momentum_mod


def _df(volumes, start="2026-01-05"):
    n = len(volumes)
    idx = pd.bdate_range(start, periods=n)
    return pd.DataFrame({"open": [100.0] * n, "high": [101.0] * n,
                         "low": [99.0] * n, "close": [100.0] * n,
                         "volume": volumes}, index=idx)


def test_volume_ratio_by_month_measures_distribution():
    # 30 flat bars then one 3x spike; the spike lands in a known month.
    vols = [100_000.0] * 30 + [300_000.0]
    bars = {"T": _df(vols)}
    per_month = vc.volume_ratio_by_month(bars)
    all_ratios = [r for v in per_month.values() for r in v]
    assert all_ratios, "expected ratios"
    assert max(all_ratios) == pytest.approx(3.0, abs=0.05)
    # Flat stretch sits at ~1.0.
    assert min(all_ratios) == pytest.approx(1.0, abs=0.05)


def test_sweep_restores_module_constant():
    """The study must never leave live detector constants mutated."""
    original = momentum_mod.VOLUME_MULT
    try:
        vc.sweep_setup("momentum_continuation", {}, None, thresholds=[1.0, 1.5])
    except Exception:
        pass
    assert momentum_mod.VOLUME_MULT == original


def test_sweep_restores_constant_even_on_error(monkeypatch):
    original = momentum_mod.VOLUME_MULT

    def boom(*a, **k):
        raise RuntimeError("detector exploded")
    monkeypatch.setattr(vc.backtest, "collect_signals", boom)
    with pytest.raises(RuntimeError):
        vc.sweep_setup("momentum_continuation", {"T": _df([1.0] * 60)},
                       None, thresholds=[1.2])
    assert momentum_mod.VOLUME_MULT == original


def test_by_month_groups_by_calendar_month():
    trades = [{"signal_date": "2024-07-15", "r": 1.0},
              {"signal_date": "2025-07-02", "r": -1.0},
              {"signal_date": "2026-01-09", "r": 2.0}]
    grouped = vc.by_month(trades)
    assert len(grouped[7]) == 2      # July across two different years
    assert len(grouped[1]) == 1


def test_optimal_threshold_ignores_small_samples():
    sweep = {
        1.0: [{"r": 0.1, "pnl_usd": 1}] * 50,     # +0.10R, big sample
        1.5: [{"r": 5.0, "pnl_usd": 50}] * 5,     # +5.00R but only 5 trades
    }
    best, exp = vc.optimal_threshold(sweep)
    assert best == 1.0                            # small sample rejected
    assert exp == pytest.approx(0.1)


def test_optimal_threshold_none_when_all_samples_tiny():
    sweep = {1.0: [{"r": 1.0, "pnl_usd": 1}] * 3}
    best, exp = vc.optimal_threshold(sweep)
    assert best is None and exp is None
