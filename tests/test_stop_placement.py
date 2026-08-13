"""
Daily-strategy stop placement (2026-08-13 NOK incident). No network.

NOK entered at 10.76 with a structural reclaim-bar stop of 10.21 (4.9%),
and was trailed out at 10.74 (0.2% — noise) within 42 minutes because the
ATR ratchet ran on 5-minute bars from the moment of entry.

  - the DETECTORS place structural stops (bar low), not ATR
  - the ratchet is INERT below +1R
  - past +1R the ratchet works again
  - risk sizing follows the wider stop (fewer shares)
"""

import pandas as pd
import pytest

import position_mgmt
import risk
from strategies.mean_reversion_reclaim import MeanReversionReclaim
from strategies.momentum_continuation import MomentumContinuation


PROFILE = {"fast_ema": 9, "slow_ema": 21, "adx_threshold": 30,
           "risk_per_trade_pct": 1.0, "rr_ratio": 3.0, "atr_multiplier": 2.0,
           "trailing_stop_type": "ATR", "trailing_stop_value": 2.0,
           "use_volume_filter": False}


def rising_intraday():
    """5-min bars grinding up — the ATR trail WOULD tighten aggressively."""
    closes = [10.76 + i * 0.004 for i in range(60)]
    idx = pd.date_range("2026-08-13 09:30", periods=len(closes), freq="5min")
    return pd.DataFrame({"open": closes, "high": [c + 0.01 for c in closes],
                         "low": [c - 0.01 for c in closes], "close": closes,
                         "volume": [100_000] * len(closes)}, index=idx)


class FakeOrder:
    def __init__(self, id):
        self.id = id


class FakeBroker:
    def __init__(self):
        self.replaced = []

    def replace_stop(self, order_id, new_stop):
        self.replaced.append((order_id, new_stop))
        return FakeOrder(f"{order_id}-r")


def nok_state(current_stop=10.21):
    return {"in_position": True, "source": "bot", "entry_price": 10.76,
            "shares_held": 28, "initial_stop": 10.21,
            "trailing_stop_price": current_stop, "stop_order_id": "stop-1",
            "timeframe": "daily"}


# ------------------------------------------------ structural placement

def test_reclaim_stop_is_signal_bar_low_not_atr():
    rows = [(100, 100.5, 99, 100, 100_000)] * 10
    for i in range(13):
        c = 98 - i
        rows.append((c + 0.5, c + 1, c - 1, c, 100_000))
    rows += [(85, 86, 84.5, 85.5, 100_000), (85.5, 88.0, 85, 87, 100_000),
             (87, 90.5, 87.5, 90, 150_000), (90, 91, 89.5, 90.5, 110_000)]
    idx = pd.date_range("2026-06-01", periods=len(rows), freq="D")
    df = pd.DataFrame(rows, index=idx,
                      columns=["open", "high", "low", "close", "volume"])
    sig = MeanReversionReclaim().detect(
        df, {"ticker": "NOK", "risk_profile": PROFILE, "config": {}})
    assert sig.stop == pytest.approx(87.5)          # reclaim bar LOW
    # A structural stop is materially wide, not noise-level.
    assert (sig.entry - sig.stop) / sig.entry > 0.02


def test_momentum_stop_is_breakout_bar_low():
    rows = [(100, 101, 99, 100, 100_000)] * 28
    rows.append((100, 105.5, 100.2, 105.0, 300_000))
    rows.append((105, 106, 104.8, 105.5, 120_000))
    idx = pd.date_range("2026-06-01", periods=len(rows), freq="D")
    df = pd.DataFrame(rows, index=idx,
                      columns=["open", "high", "low", "close", "volume"])
    sig = MomentumContinuation().detect(
        df, {"ticker": "X", "risk_profile": PROFILE, "config": {}})
    assert sig.stop == pytest.approx(100.2)         # breakout bar LOW


# ------------------------------------------------ the +1R gate

def test_ratchet_is_inert_below_1r_the_nok_case():
    broker = FakeBroker()
    positions = {"NOK": nok_state()}
    # Price up 0.9% — nowhere near +1R (1R = 0.55 = +5.1%).
    changed = position_mgmt.maybe_ratchet_stop(
        broker, positions, "NOK", positions["NOK"], rising_intraday(),
        PROFILE, current_price=10.86)
    assert changed is False
    assert broker.replaced == []                     # leg untouched
    assert positions["NOK"]["trailing_stop_price"] == 10.21   # structure holds


def test_ratchet_resumes_past_1r():
    broker = FakeBroker()
    positions = {"NOK": nok_state()}
    # 1R = 10.76 - 10.21 = 0.55 -> +1R is 11.31.
    changed = position_mgmt.maybe_ratchet_stop(
        broker, positions, "NOK", positions["NOK"], rising_intraday(),
        PROFILE, current_price=11.40)
    assert changed is True
    assert len(broker.replaced) == 1
    _, new_stop = broker.replaced[0]
    assert new_stop > 10.21 and new_stop < 11.40


def test_r_multiple_math():
    state = nok_state()
    assert position_mgmt.r_multiple(state, 10.76) == pytest.approx(0.0)
    assert position_mgmt.r_multiple(state, 11.31) == pytest.approx(1.0)
    assert position_mgmt.r_multiple(state, 10.21) == pytest.approx(-1.0)


def test_r_multiple_uses_initial_stop_not_the_moved_one():
    """Once trailing starts, the R yardstick must stay the ORIGINAL risk."""
    state = nok_state(current_stop=11.00)            # already ratcheted up
    assert position_mgmt.r_multiple(state, 11.31) == pytest.approx(1.0)


def test_missing_geometry_does_not_block_legacy_positions():
    state = {"in_position": True, "source": "bot", "stop_order_id": "s1",
             "trailing_stop_price": 0.0}
    assert position_mgmt.r_multiple(state, 10.0) is None
    broker = FakeBroker()
    # No geometry -> gate cannot apply; behaviour falls back to trailing.
    position_mgmt.maybe_ratchet_stop(broker, {"X": state}, "X", state,
                                     rising_intraday(), PROFILE, 10.86)


def test_ceo_positions_still_never_ratcheted():
    broker = FakeBroker()
    state = dict(nok_state(), source="ceo")
    assert position_mgmt.maybe_ratchet_stop(
        broker, {"NOK": state}, "NOK", state, rising_intraday(),
        PROFILE, current_price=99.0) is False
    assert broker.replaced == []


# ------------------------------------------------ sizing follows the stop

def test_wider_structural_stop_means_fewer_shares():
    equity = 2000.0
    tight = risk.position_size(equity, 1.0, 10.76, 10.69,   # 0.65% noise stop
                               position_cap_pct=0.25)
    structural = risk.position_size(equity, 1.0, 10.76, 10.21,  # 4.9% structure
                                    position_cap_pct=0.25)
    assert structural < tight                        # wider stop -> fewer shares
    # And the dollar risk stays inside the 1% budget either way.
    assert structural * (10.76 - 10.21) <= equity * 0.01 + 1e-9
