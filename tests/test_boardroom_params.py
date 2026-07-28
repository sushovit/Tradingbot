"""
Boardroom-ratified parameters (2026-07-28). No network.

  1. Targets 3R: rr_ratio 3.0 in both profiles; 3R floors in momentum and
     reclaim (reclaim keeps a structural prior-high target above 3R)
  2. adx_threshold 30 in ALL profiles
  3. Rule #5: reclaim EXEMPT from the spy_bearish block, tagged
     'chop_reclaim'; continuation strategies stay regime-blocked
"""

import json
import sqlite3

import pandas as pd
import pytest

from strategies.momentum_continuation import MomentumContinuation, MIN_TARGET_R as MOM_R
from strategies.mean_reversion_reclaim import MeanReversionReclaim, MIN_TARGET_R as REC_R


def config():
    with open("bot_config.json") as f:
        return json.load(f)


# ------------------------------------------------------------------ 1 & 2

def test_profiles_use_3r_targets_and_adx_30():
    profiles = config()["risk_profiles"]
    assert set(profiles) == {"Aggressive", "Moderate"}
    for name, p in profiles.items():
        assert p["rr_ratio"] == 3.0, f"{name} rr_ratio not 3.0"
        assert p["adx_threshold"] == 30, f"{name} adx_threshold not 30"


def test_strategy_target_floors_are_3r():
    assert MOM_R == 3.0
    assert REC_R == 3.0


def test_reclaim_structural_target_wins_when_above_3r():
    """A prior high far above the 3R floor must survive (spec: keep the
    structural target when it exceeds 3R)."""
    rows = [(100, 160.0, 99, 100, 100_000)] * 10          # 20d high = 160
    for i in range(13):
        c = 98 - i
        rows.append((c + 0.5, c + 1, c - 1, c, 100_000))
    rows.append((85, 86, 84.5, 85.5, 100_000))
    rows.append((85.5, 88.0, 85, 87, 100_000))
    rows.append((87, 90.5, 87.5, 90, 150_000))            # reclaim bar
    rows.append((90, 91, 89.5, 90.5, 110_000))
    idx = pd.date_range("2026-06-01", periods=len(rows), freq="D")
    df = pd.DataFrame(rows, index=idx,
                      columns=["open", "high", "low", "close", "volume"])
    profile = {"fast_ema": 9, "slow_ema": 21, "adx_threshold": 30,
               "risk_per_trade_pct": 1.0, "rr_ratio": 3.0, "atr_multiplier": 2.0,
               "trailing_stop_type": "ATR", "trailing_stop_value": 2.0,
               "use_volume_filter": False}
    signal = MeanReversionReclaim().detect(
        df, {"ticker": "T", "risk_profile": profile, "config": {}})
    r = signal.entry - signal.stop
    assert signal.target == pytest.approx(160.0)          # structural high
    assert signal.target > signal.entry + 3 * r           # ...above the floor


# ------------------------------------------------------------------ 3

def test_rule5_exemption_is_configured():
    assert "mean_reversion_reclaim" in config().get("spy_filter_exempt", [])
    # Continuation strategies must NOT be exempt.
    for s in ("trend_continuation", "momentum_continuation"):
        assert s not in config().get("spy_filter_exempt", [])


def test_chop_reclaim_tag_is_an_acceptance_not_a_rejection(temp_journal):
    temp_journal.log_signal_tag("IREN", "mean_reversion_reclaim",
                                "chop_reclaim", "SPY below 20-EMA, entry 39.80")
    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM decisions WHERE source='rules'").fetchone()
    conn.close()
    verdict = json.loads(row["verdict"])
    assert row["approved"] == 1                    # taken, NOT passed
    assert verdict["tag"] == "chop_reclaim"
    assert verdict["rejection_reason"] is None


def test_chop_reclaim_report_tracks_outcomes(temp_journal):
    did = temp_journal.log_signal_tag("IREN", "mean_reversion_reclaim",
                                      "chop_reclaim", "x")
    rep = temp_journal.chop_reclaim_report()
    assert rep["tagged"] == 1 and rep["closed"] == 0
    tid = temp_journal.log_trade("IREN", "SELL", 5, 42.0, pnl_usd=11.0)
    temp_journal.link_outcome(did, tid, 11.0, 5.5)
    rep = temp_journal.chop_reclaim_report()
    assert rep["closed"] == 1
    assert rep["realized_usd"] == pytest.approx(11.0)
    assert rep["tickers"] == ["IREN"]
