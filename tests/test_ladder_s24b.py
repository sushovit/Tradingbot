"""
S24b — owner ladder for daily positions (2026-10-14).

Once a completed 5-minute close reaches +0.50%: stop -> entry. From then on
stop = max(stop, highest close since entry x 0.995). Never lowered; at least
0.1% below the last price; original stop and 3R target unchanged.
Plus the weekly report's alternative-rule replay.
"""

import json
from datetime import date, datetime, timezone

import pandas as pd
import pytest

import lock_replay
import position_mgmt as pm
import trade_table as tt

P = pm.LADDER_DEFAULTS
NOW = datetime(2026, 10, 15, 15, 0, 30, tzinfo=timezone.utc)
PROFILE = {"trailing_stop_type": "ATR", "trailing_stop_value": 2.5,
           "daily_trailing": "ladder", "daily_lock": {"start_pct": 0.50,
                                                      "cushion_pct": 0.50}}


class Order:
    def __init__(self, oid):
        self.id = oid


class FakeBroker:
    def __init__(self):
        self.replaced = []

    def replace_stop(self, order_id, price):
        self.replaced.append((order_id, price))
        return Order(f"stop-{len(self.replaced)}")


def txn(**over):
    state = {"in_position": True, "source": "bot", "timeframe": "daily",
             "entry_price": 280.25, "initial_stop": 278.0,
             "trailing_stop_price": 278.0, "profit_target_price": 298.69,
             "stop_order_id": "stop-0", "target_order_id": "tgt-0",
             "shares_held": 4}
    state.update(over)
    return state


def bars(*closes, now=NOW):
    end = pd.Timestamp(now).floor("5min") - pd.Timedelta(minutes=5)
    idx = [end - pd.Timedelta(minutes=5 * (len(closes) - 1 - i))
           for i in range(len(closes))]
    return pd.DataFrame({"open": closes, "high": closes, "low": closes,
                         "close": closes}, index=pd.DatetimeIndex(idx))


def run(state, df, last):
    broker, journaled, alerts = FakeBroker(), [], []
    persisted = []
    changed = pm.maybe_pct_lock(
        broker, {"TXN": state}, "TXN", state, df, PROFILE, last, NOW,
        persist=lambda p: persisted.append(json.loads(json.dumps(p))),
        journal_raise=lambda *a: journaled.append(a),
        alert=lambda *a: alerts.append(a))
    return changed, broker, journaled, persisted, alerts


# ================================================== the owner's TXN numbers

@pytest.mark.parametrize("high, stop, step", [
    (284.23, 282.82, "breakeven"),     # 282.815 x 1.005 = 284.229
    (284.93, 283.51, "trail"),         # 284.93 x 0.995 = 283.505, up
])
def test_the_owner_examples_at_entry_282_815(high, stop, step):
    assert pm.ladder_target(282.815, high, high, 1e9, P) == (stop, step)


def test_below_start_nothing():
    assert pm.ladder_target(282.815, 284.22, 284.22, 1e9, P) == (None, None)


def test_txn_at_its_real_fill():
    f = lambda c: pm.ladder_target(280.25, c, c, 1e9, P)
    assert f(281.65) == (None, None)           # 280.25 x 1.005 = 281.651
    assert f(281.66)[0] == 280.26              # 281.66 x .995 just over entry
    assert f(283.00) == (281.59, "trail")


# ================================================== the live function

def test_breakeven_then_trail_then_never_down():
    state = txn()
    changed, broker, journaled, persisted, alerts = run(state, bars(281.7), 281.8)
    assert changed and broker.replaced == [("stop-0", 280.30)]   # 281.7 x .995 = 280.2915, up
    assert "ladder" in journaled[0][3] and alerts[0][3] == "trail"
    run(state, bars(281.7, 284.0), 284.1)
    assert state["trailing_stop_price"] == 282.58                # 284 x .995
    changed, broker, _, _, _ = run(state, bars(282.9), 282.9)    # pullback
    assert changed is False and broker.replaced == []
    assert state["trailing_stop_price"] == 282.58
    assert state["highest_close"] == 284.0


def test_exact_breakeven_when_the_cushion_is_still_below_entry():
    state = txn(entry_price=282.815, initial_stop=278.0)
    changed, broker, _, _, alerts = run(state, bars(284.23), 284.3)
    assert broker.replaced == [("stop-0", 282.82)] and alerts[0][3] == "breakeven"


def test_kept_at_least_0_1pct_below_market():
    state = txn()
    changed, broker, _, _, _ = run(state, bars(284.0), 282.0)
    assert broker.replaced == [("stop-0", 281.71)]   # floor(282 x .999), not 282.58
    assert "capped" in state["lock_step"]


def test_restart_is_a_no_op():
    state = txn()
    _, _, _, persisted, _ = run(state, bars(284.0), 284.1)
    changed, broker, journaled, _, alerts = run(persisted[-1]["TXN"], bars(284.0), 284.1)
    assert changed is False and broker.replaced == [] and journaled == []


def test_original_stop_and_target_are_never_touched():
    state = txn()
    run(state, bars(290.0), 290.1)
    assert state["initial_stop"] == 278.0 and state["original_stop"] == 278.0
    assert state["profit_target_price"] == 298.69
    assert state["target_order_id"] == "tgt-0"


def test_shipped_config_is_the_ladder():
    cfg = json.load(open("bot_config.json", encoding="utf-8"))
    for name in ("Aggressive", "Moderate"):
        prof = cfg["risk_profiles"][name]
        assert prof["daily_trailing"] == "ladder"
        assert pm.ladder_params(prof) == {"start_pct": 0.5, "cushion_pct": 0.5,
                                          "market_gap_pct": 0.1}
        assert prof["trailing_stop_type"] == "ATR"      # intraday unchanged


def test_the_worker_routes_both_daily_locks():
    with open("streamlit_app.py", encoding="utf-8") as f:
        src = f.read()
    assert "if _ttype in position_mgmt.DAILY_LOCKS:" in src
    assert pm.DAILY_LOCKS == ("pct_lock", "ladder")


# ================================================== weekly alternatives

def m5(rows, start="2026-10-14 13:35"):
    idx = pd.date_range(start=pd.Timestamp(start, tz="UTC"), periods=len(rows),
                        freq="5min")
    o, h, l, c = zip(*rows)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c}, index=idx)


def test_replay_old_plus_one_r_and_cushion_025():
    """Entry 100, stop 95 (R 5), target 115. Price runs to 105.5 then falls
    to 99: the +1R rule exits at breakeven 100; the 0.25% ladder trails at
    105.5 x 0.9975 = 105.24."""
    rows = [(100, 101, 99.5, 101), (101, 105.6, 101, 105.5),
            (105.4, 105.4, 99, 99.5)]      # opens above the trail, then falls
    bars_ = m5(rows)
    one_r = lock_replay.replay(bars_, "2026-10-14 09:30:00", 100.0, 95.0,
                               115.0, lock_replay.plus_one_r_policy)
    cush = lock_replay.replay(bars_, "2026-10-14 09:30:00", 100.0, 95.0,
                              115.0, lock_replay.ladder_policy(0.25))
    assert one_r[0] == 100.0 and one_r[2] == "stop"
    assert cush[0] == 105.24 and cush[2] == "stop"


def test_replay_marks_an_unclosed_trade_open():
    bars_ = m5([(100, 101, 99.5, 100.5)] * 3)
    px, ts, how = lock_replay.replay(bars_, "2026-10-14 09:30:00", 100.0, 95.0,
                                     115.0, lock_replay.plus_one_r_policy)
    assert (px, ts, how) == (100.5, None, "open")


def test_the_weekly_line_shows_both_alternatives():
    alts = {"cushion 0.25%": (12.3, 2, 0), "old +1R rule": (-3.1, 2, 1)}
    lines = tt.weekly_lines([], date(2026, 10, 16), 5000.0, alts)
    text = "\n".join(lines)
    assert "cushion 0.25%: +$12.30 (2 tr)" in text
    assert "old +1R rule: -$3.10 (2 tr, 1 open)" in text
    assert max(len(l) for l in lines) <= 50


def test_alternatives_skip_trades_without_a_stop_or_bars():
    trades = [{"ticker": "A", "qty": 1, "buy": 100.0, "buy_ts": "2026-10-14 09:30:00",
               "sell_ts": "2026-10-14 11:00:00", "stop": None, "target": 115.0},
              {"ticker": "B", "qty": 2, "buy": 100.0, "buy_ts": "2026-10-14 09:30:00",
               "sell_ts": "2026-10-14 11:00:00", "stop": 95.0, "target": 115.0}]
    out = lock_replay.alternatives(trades, lambda t, s: None)
    assert out == {"cushion 0.25%": (0.0, 0, 0), "old +1R rule": (0.0, 0, 0)}


def test_daily_setups_are_recognised():
    assert tt.is_daily_setup("mean_reversion_reclaim")
    assert tt.is_daily_setup("CEO momentum_continuation")


def test_a_bar_opening_through_the_trail_fills_at_the_open():
    rows = [(100, 101, 99.5, 101), (101, 105.6, 101, 105.5), (105, 105, 99, 99.5)]
    px, _, how = lock_replay.replay(m5(rows), "2026-10-14 09:30:00", 100.0,
                                    95.0, 115.0, lock_replay.ladder_policy(0.25))
    assert (px, how) == (105.0, "stop (gap)")
