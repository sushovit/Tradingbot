"""
S24 — percent lock for daily positions (CEO + owner 2026-10-14).

On the last COMPLETED 5-minute close, from the BROKER's average fill:
  step1 +0.75% -> stop halfway from the original stop to entry
  step2 +1.25% -> stop at entry +0.1%
  trail once a close reached +2% -> highest close since entry -1%
Never lowered; never within 0.1% of the last price; restart is a no-op.
"""

import json
from datetime import datetime, timezone

import pandas as pd
import pytest

import position_mgmt as pm
import review_bot

P = pm.PCT_LOCK_DEFAULTS
NOW = datetime(2026, 10, 15, 15, 0, 30, tzinfo=timezone.utc)      # 11:00 ET
PROFILE = {"trailing_stop_type": "ATR", "trailing_stop_value": 2.5,
           "daily_trailing": "pct_lock", "daily_pct_lock": dict(P)}


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
             "setup": "mean_reversion_reclaim", "entry_price": 280.25,
             "initial_stop": 278.0, "trailing_stop_price": 278.0,
             "stop_order_id": "stop-0", "shares_held": 4, "decision_id": 12409}
    state.update(over)
    return state


def bars(*closes, now=NOW):
    """Completed 5-minute bars (start-stamped) ending just before `now`."""
    end = pd.Timestamp(now).floor("5min") - pd.Timedelta(minutes=5)
    idx = [end - pd.Timedelta(minutes=5 * (len(closes) - 1 - i))
           for i in range(len(closes))]
    return pd.DataFrame({"open": closes, "high": closes, "low": closes,
                         "close": closes}, index=pd.DatetimeIndex(idx))


def run(state, df, last, broker_entry=None, profile=PROFILE):
    broker, journaled, persisted, alerts, order = FakeBroker(), [], [], [], []
    positions = {"TXN": state}

    def persist(p):
        order.append("persist")
        persisted.append(json.loads(json.dumps(p)))

    def journal_raise(t, q, px, why):
        order.append("journal")
        journaled.append((t, q, px, why))
    changed = pm.maybe_pct_lock(
        broker, positions, "TXN", state, df, profile, last, NOW,
        broker_entry=broker_entry, persist=persist,
        journal_raise=journal_raise,
        alert=lambda *a: alerts.append(a))
    return changed, broker, journaled, persisted, alerts, order


# ================================================== the CEO's TXN arithmetic

@pytest.mark.parametrize("close, stop, step", [
    (284.94, 280.41, "step1"),      # 282.815 x 1.0075 = 284.936: first cent
    (286.36, 283.10, "step2"),      # 282.815 x 1.0125 = 286.350; 283.0978 up
    (290.00, 287.10, "trail"),      # 290 x 0.99
])
def test_the_order_examples_at_entry_282_815(close, stop, step):
    assert pm.pct_lock_target(282.815, 278.0, close, close, 1e9, P) == (stop, step)


def test_the_example_triggers_are_a_fraction_of_a_cent_short():
    """284.93 and 286.35 are the example thresholds rounded DOWN; exactly
    at them the close is still below entry x 1.0075 / x 1.0125."""
    assert pm.pct_lock_target(282.815, 278.0, 284.93, 284.93, 1e9, P) == (None, None)
    assert pm.pct_lock_target(282.815, 278.0, 286.35, 286.35, 1e9, P)[1] == "step1"


def test_txn_at_its_real_broker_fill_280_25():
    """TXN filled at 280.25 (broker order and position); the lock measures
    from that, so every trigger sits lower than in the examples."""
    f = lambda c: pm.pct_lock_target(280.25, 278.0, c, c, 1e9, P)
    assert f(282.35) == (None, None)                 # 280.25 x 1.0075 = 282.352
    assert f(282.36) == (279.13, "step1")            # 278 + 0.5 x 2.25
    assert f(283.76) == (280.54, "step2")            # 280.25 x 1.001, up
    assert f(285.86) == (283.01, "trail")            # 285.86 x 0.99, up


# ================================================== the live function

def test_below_the_first_trigger_nothing_moves():
    changed, broker, journaled, _, alerts, _ = run(txn(), bars(281.0, 282.0), 282.1)
    assert changed is False and broker.replaced == [] and journaled == []


def test_step1_step2_and_trail_raise_in_turn():
    state = txn()
    changed, broker, journaled, persisted, alerts, order = run(
        state, bars(281.0, 282.5), 282.6)
    assert changed and broker.replaced == [("stop-0", 279.13)]
    assert state["lock_step"] == "step1" and "step1" in journaled[0][3]
    assert order[-2:] == ["journal", "persist"]
    assert alerts[0][:4] == ("TXN", 278.0, 279.13, "step1")
    changed, broker, _, _, _, _ = run(state, bars(283.9), 284.0)
    assert changed and broker.replaced == [("stop-1", 280.54)]
    changed, broker, _, _, _, _ = run(state, bars(286.0), 286.1)
    # (each run() uses a fresh fake broker, so the new leg is stop-1 again)
    assert changed and broker.replaced == [("stop-1", 283.14)]   # 286 x .99


def test_the_trail_follows_up_and_never_down():
    state = txn()
    run(state, bars(286.0), 286.1)
    assert state["trailing_stop_price"] == 283.14
    run(state, bars(286.0, 289.0), 289.1)
    assert state["trailing_stop_price"] == 286.11          # 289 x .99
    changed, broker, _, _, _, _ = run(state, bars(287.0), 287.0)   # pullback
    assert changed is False and broker.replaced == []
    assert state["trailing_stop_price"] == 286.11
    assert state["highest_close"] == 289.0


def test_never_lowers_an_existing_stop():
    changed, broker, _, _, _, _ = run(txn(trailing_stop_price=284.0),
                                      bars(286.0), 286.1)
    assert changed is False and broker.replaced == []


def test_keeps_the_stop_at_least_0_1pct_below_market():
    """The close earned step2 (280.54) but price has dropped to 280.60: the
    stop goes to 280.31 (0.1% under), not at the market."""
    state = txn()
    changed, broker, journaled, _, _, _ = run(state, bars(283.9), 280.60)
    assert changed and broker.replaced == [("stop-0", 280.31)]
    assert "capped" in state["lock_step"]


def test_a_restart_is_a_no_op():
    state = txn()
    _, _, _, persisted, _, _ = run(state, bars(283.9), 284.0)
    reloaded = persisted[-1]["TXN"]
    changed, broker, journaled, _, alerts, _ = run(reloaded, bars(283.9), 284.0)
    assert changed is False and broker.replaced == [] and journaled == []
    assert alerts == []


def test_entry_is_the_broker_fill_not_the_record():
    """A record holding the signal price (282.815) is corrected to the
    broker's 280.25 before the lock is computed - and persisted."""
    state = txn(entry_price=282.815)
    changed, broker, _, persisted, _, _ = run(state, bars(282.5), 282.6,
                                              broker_entry=280.25)
    assert state["entry_price"] == 280.25
    assert broker.replaced == [("stop-0", 279.13)]      # step1 off 280.25
    assert persisted[-1]["TXN"]["entry_price"] == 280.25


def test_only_completed_bars_count():
    """A still-forming bar at 290 must not trigger anything."""
    df = bars(281.0)
    forming = pd.DataFrame({"open": [290.0], "high": [290.0], "low": [290.0],
                            "close": [290.0]},
                           index=pd.DatetimeIndex([pd.Timestamp(NOW).floor("5min")]))
    changed, broker, _, _, _, _ = run(txn(), pd.concat([df, forming]), 290.0)
    assert changed is False and broker.replaced == []


def test_ceo_and_intraday_positions_are_untouched():
    for state in (txn(source="ceo"), txn(timeframe="intraday")):
        changed, broker, _, _, _, _ = run(state, bars(290.0), 290.0)
        assert changed is False and broker.replaced == []


def test_the_atr_ratchet_never_acts_on_a_pct_lock_position():
    broker = FakeBroker()
    idx = pd.date_range("2026-10-15 13:30", periods=60, freq="5min", tz="UTC")
    df = pd.DataFrame({"open": 300.0, "high": 301.0, "low": 299.0,
                       "close": 300.0, "volume": 1}, index=idx)
    state = txn(reached_1r=True)
    assert pm.maybe_ratchet_stop(broker, {"TXN": state}, "TXN", state, df,
                                 PROFILE, 300.0) is False
    assert broker.replaced == []


# ================================================== config and wiring

def test_config_parsing_and_defaults():
    assert pm.pct_lock_params({}) == P
    custom = pm.pct_lock_params({"daily_pct_lock": {"trail_pct": "1.5",
                                                    "bogus": 9,
                                                    "step2_stop_pct": "x"}})
    assert custom["trail_pct"] == 1.5 and custom["step2_stop_pct"] == 0.1
    assert "bogus" not in custom


def test_shipped_config_is_the_ceo_rule():
    cfg = json.load(open("bot_config.json", encoding="utf-8"))
    for name in ("Aggressive", "Moderate"):
        prof = cfg["risk_profiles"][name]
        assert prof["daily_trailing"] == "pct_lock"
        assert pm.pct_lock_params(prof) == {
            "step1_trigger_pct": 0.75, "step1_risk_fraction": 0.5,
            "step2_trigger_pct": 1.25, "step2_stop_pct": 0.1,
            "trail_trigger_pct": 2.0, "trail_pct": 1.0, "market_gap_pct": 0.1}
        assert prof["trailing_stop_type"] == "ATR"        # intraday unchanged


def test_the_worker_runs_the_pct_lock_every_cycle_from_the_broker_fill():
    with open("streamlit_app.py", encoding="utf-8") as f:
        src = f.read()
    block = src[src.index("position_mgmt.maybe_pct_lock("):][:900]
    assert "broker_entry=broker_entries.get(ticker)" in block
    assert '"TIGHTEN_STOP"' in block
    assert "persist=write_positions" in block
    assert "alert=trade_alerts.alert_stop_raise" in block
    assert "broker_entries = {p.symbol: float(p.avg_entry_price)" in src


def test_the_stop_raise_alert_formats_and_never_raises():
    import trade_alerts
    e = trade_alerts.stop_raise_embed("TXN", 278.0, 279.13, "step1", 280.25,
                                      282.6)
    f = dict(e["fields"])
    assert e["title"] == "🔒 STOP RAISED TXN"
    assert f["Stop"] == "$278.00 -> $279.13" and f["Step"] == "step1"
    assert f["Entry (broker fill)"] == "$280.25"
    trade_alerts.alert_stop_raise(None, None, None, None, None, None)  # no raise


def test_the_review_prompt_states_the_percent_lock():
    prompt = review_bot.REVIEW_SYSTEM_PROMPT
    assert "PERCENT LOCK" in prompt and "daily_pct_lock" in prompt
    assert "BROKER's average fill" in prompt
