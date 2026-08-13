"""
P0 fixes 2026-08-13: duplicate-worker incident. No network.

  A. second launch against a fresh heartbeat exits non-zero and places
     NO orders
  B. volume thresholds come from config (1.3x both setups)
  C. order-side dedupe refuses when the ACCOUNT already shows a position
     or a working order
  + momentum no-room patch, exit-tier inheritance and backfill
"""

import json
import sqlite3

import pandas as pd
import pytest

import position_mgmt
import prompts
import run_worker
from broker import BrokerError
from strategies.momentum_continuation import MomentumContinuation
from strategies.mean_reversion_reclaim import MeanReversionReclaim


# ------------------------------------------------------------------ A

def test_second_launch_exits_nonzero_and_places_no_orders(tmp_path, monkeypatch,
                                                          capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot_status.log").write_text("[now] Cycle #42")     # fresh
    (tmp_path / "bot.run").write_text("4242")

    imported, locked = [], []
    monkeypatch.setattr(run_worker, "write_lock",
                        lambda *a, **k: locked.append(1))
    real_import = __import__

    def guard_import(name, *a, **k):
        if name == "streamlit_app":
            imported.append(name)          # would start the trading loop
        return real_import(name, *a, **k)
    monkeypatch.setattr("builtins.__import__", guard_import)

    rc = run_worker.main([])
    assert rc != 0
    assert imported == []                  # worker never started -> no orders
    assert locked == []
    assert "refusing to start" in capsys.readouterr().out


# ------------------------------------------------------------------ B

def test_config_volume_multipliers_are_1_3():
    with open("bot_config.json") as f:
        mults = json.load(f)["volume_multipliers"]
    assert mults["momentum_continuation"] == 1.3
    assert mults["mean_reversion_reclaim"] == 1.3


def momentum_df(vol_ratio):
    """Breakout bar at `vol_ratio` x the 20-bar average volume."""
    rows = [(100, 101, 99, 100, 100_000)] * 28
    rows.append((100, 105.5, 100.2, 105.0, int(100_000 * vol_ratio)))
    rows.append((105, 106, 104.8, 105.5, 120_000))
    idx = pd.date_range("2026-06-01", periods=len(rows), freq="D")
    return pd.DataFrame(rows, index=idx,
                        columns=["open", "high", "low", "close", "volume"])


def test_orcl_case_1_42x_passes_at_1_3_and_failed_at_1_5():
    """This morning's ORCL printed 1.42x and was rejected on stale 1.5x."""
    profile = {"fast_ema": 9, "slow_ema": 21, "adx_threshold": 30,
               "risk_per_trade_pct": 1.0, "rr_ratio": 3.0, "atr_multiplier": 2.0,
               "trailing_stop_type": "ATR", "trailing_stop_value": 2.0,
               "use_volume_filter": True}
    df = momentum_df(1.42)

    cfg_new = {"volume_multipliers": {"momentum_continuation": 1.3}}
    res_new = MomentumContinuation().detect(
        df, {"ticker": "ORCL", "risk_profile": profile, "config": cfg_new})
    assert res_new.__class__.__name__ == "Signal", "1.42x must pass at 1.3x"

    cfg_old = {"volume_multipliers": {"momentum_continuation": 1.5}}
    res_old = MomentumContinuation().detect(
        df, {"ticker": "ORCL", "risk_profile": profile, "config": cfg_old})
    assert res_old.__class__.__name__ == "Rejection"
    assert res_old.filter_name == "volume_low"


def test_reclaim_reads_config_multiplier():
    cfg = {"volume_multipliers": {"mean_reversion_reclaim": 9.0}}   # absurd
    rows = [(100, 100.5, 99, 100, 100_000)] * 10
    for i in range(13):
        c = 98 - i
        rows.append((c + 0.5, c + 1, c - 1, c, 100_000))
    rows += [(85, 86, 84.5, 85.5, 100_000), (85.5, 88.0, 85, 87, 100_000),
             (87, 90.5, 87.5, 90, 150_000), (90, 91, 89.5, 90.5, 110_000)]
    idx = pd.date_range("2026-06-01", periods=len(rows), freq="D")
    df = pd.DataFrame(rows, index=idx,
                      columns=["open", "high", "low", "close", "volume"])
    res = MeanReversionReclaim().detect(
        df, {"ticker": "T", "risk_profile": {}, "config": cfg})
    assert res.__class__.__name__ == "Rejection"      # config threshold applied
    assert res.filter_name == "volume_low"


# ------------------------------------------------------------------ C

class _Pos:
    def __init__(self, symbol, qty):
        self.symbol, self.qty = symbol, qty


class _Ord:
    def __init__(self, symbol, order_type="stop"):
        self.symbol, self.order_type = symbol, order_type


class DupBroker:
    def __init__(self, positions=(), orders=(), fail=None):
        self._p, self._o, self._fail = list(positions), list(orders), fail

    def get_positions(self):
        if self._fail == "positions":
            raise BrokerError("account unreachable")
        return self._p

    def get_live_orders(self, ticker=None):
        if self._fail == "orders":
            raise BrokerError("orders unreachable")
        return [o for o in self._o if ticker is None or o.symbol == ticker]


def test_dedupe_blocks_existing_position():
    reason = position_mgmt.duplicate_entry_exists(
        DupBroker(positions=[_Pos("NOK", 28)]), "NOK")
    assert reason and "position already open" in reason


def test_dedupe_blocks_working_order():
    reason = position_mgmt.duplicate_entry_exists(
        DupBroker(orders=[_Ord("ORCL", "stop"), _Ord("ORCL", "limit")]), "ORCL")
    assert reason and "working order" in reason


def test_dedupe_allows_clean_ticker():
    assert position_mgmt.duplicate_entry_exists(DupBroker(), "AMD") is None


def test_dedupe_refuses_when_it_cannot_verify():
    """A missed trade beats a double fill."""
    assert position_mgmt.duplicate_entry_exists(
        DupBroker(fail="positions"), "NOK") is not None
    assert position_mgmt.duplicate_entry_exists(
        DupBroker(fail="orders"), "NOK") is not None


# ------------------------------------------------- momentum no-room patch

def test_momentum_no_room_guidance():
    p = prompts.build_gatekeeper_user_prompt(
        ticker="NBIS", candle_data_str="c", adx_val=30.0, rsi_val=60.0,
        ema_spread_pct=0.3, volume_trend="increasing", crossover_count=1,
        dist_to_resistance_pct=0.0, entry_price=100.0, stop_price=95.0,
        target_price=115.0, rr_ratio=3.0, interval_mins=5, fast_ema=9,
        slow_ema=21, news_str="none", setup_name="momentum_continuation")
    assert "DEFINITIONAL" in p
    assert "circular" in p
    assert "RSI is overextended" in p          # still a valid factor


# ------------------------------------------------- exit tier inheritance

def test_exit_inherits_entry_tier(temp_journal):
    d = temp_journal.log_decision("PLTR", "momentum_continuation", {},
                                  {"approved": True, "conviction_score": 75})
    temp_journal.log_trade("PLTR", "BUY", 1, 100.0, decision_id=d, tier="B")
    temp_journal.record_exit("PLTR", 1, 90.0, "Stop Loss", decision_id=d,
                             broker_order_id="x1", entry_price=100.0)
    assert temp_journal.tier_realized_pnl("B") == pytest.approx(-10.0)
    assert temp_journal.tier_realized_pnl("A") == pytest.approx(0.0)


def test_exit_tier_backfill_migration(temp_journal):
    d = temp_journal.log_decision("PLTR", "momentum_continuation", {},
                                  {"approved": True, "conviction_score": 75})
    temp_journal.log_trade("PLTR", "BUY", 1, 100.0, decision_id=d, tier="B")
    # Legacy SELL written before record_exit knew about tiers -> booked to A.
    temp_journal.log_trade("PLTR", "SELL", 1, 90.0, pnl_usd=-10.0,
                           decision_id=d, tier="A")
    assert temp_journal.tier_realized_pnl("A") == pytest.approx(-10.0)

    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.execute("DELETE FROM meta WHERE key LIKE 'mig_%'")
    conn.commit()
    conn.close()
    temp_journal.run_data_migrations()

    assert temp_journal.tier_realized_pnl("B") == pytest.approx(-10.0)
    assert temp_journal.tier_realized_pnl("A") == pytest.approx(0.0)
