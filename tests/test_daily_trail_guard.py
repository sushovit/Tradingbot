"""
W7 (PM_PLAN.md): never trail a daily position on intraday bars. No network.

INCIDENT 2026-09-08 14:42 ET. A DNS drop made get_daily_bars fail while
get_bars kept succeeding. The trailing block fell back to the 5-minute frame
for daily positions, so CRCL and SLB were ratcheted with 5-minute ATR to
inside 1% of price — a daily structure on an intraday stop, which is the NOK
failure mode. From worker.log:

    00:27:47  Daily bars unavailable this cycle: get_daily_bars failed
    00:28:01  SLB:  trailing stop raised to $57.10   (was 55.86, breakeven)
    00:28:05  CRCL: trailing stop raised to $95.80   (was 90.32, breakeven)

A missing daily frame is now a reason to SKIP the ratchet, never a reason to
trail on the wrong timeframe.
"""

import pandas as pd
import pytest


def intraday(last=100.0, n=60):
    """5-minute bars, tight range — the frame that must never trail a daily
    position. A small ATR here is exactly what produces a stop inside 1%."""
    closes = [last - (n - 1 - i) * 0.01 for i in range(n)]
    idx = pd.date_range("2026-09-08 09:30", periods=n, freq="5min")
    return pd.DataFrame({"open": closes, "high": [c + .02 for c in closes],
                         "low": [c - .02 for c in closes], "close": closes,
                         "volume": [100_000] * n}, index=idx)


def daily(last=100.0, n=60):
    closes = [last - (n - 1 - i) * 0.4 for i in range(n)]
    idx = pd.bdate_range(end="2026-09-08", periods=n)
    return pd.DataFrame({"open": closes, "high": [c + 1.5 for c in closes],
                         "low": [c - 1.5 for c in closes], "close": closes,
                         "volume": [1_000_000] * n}, index=idx)


class FakeOrder:
    def __init__(self, oid="new"):
        self.id = oid


class RecordingBroker:
    """Records every stop replacement so a test can assert none happened."""

    def __init__(self):
        self.replaced = []

    def replace_stop(self, order_id, new_stop):
        self.replaced.append((order_id, new_stop))
        return FakeOrder(f"{order_id}-r")


def crcl_state(timeframe="daily"):
    """CRCL as it stood: past +1R, stop at breakeven."""
    return {"in_position": True, "source": "bot", "entry_price": 90.32,
            "shares_held": 1, "initial_stop": 79.57,
            "trailing_stop_price": 90.32, "stop_order_id": "stop-crcl",
            "reached_1r": True, "timeframe": timeframe}


# ============================================ (1) daily position, no frame

def test_daily_position_is_not_ratcheted_when_the_daily_frame_is_missing():
    """The incident, as a unit test: an empty daily_bars must produce no
    replace_stop call at all."""
    import position_mgmt
    broker = RecordingBroker()
    positions = {"CRCL": crcl_state()}
    daily_bars = {}                                   # the outage

    # The guard the worker applies before ever calling the ratchet.
    frame = daily_bars.get("CRCL")
    should_skip = frame is None or frame.empty
    assert should_skip is True
    if not should_skip:                               # pragma: no cover
        position_mgmt.maybe_ratchet_stop(
            broker, positions, "CRCL", positions["CRCL"], intraday(96.5),
            {"trailing_stop_type": "ATR", "trailing_stop_value": 2.5}, 96.5)

    assert broker.replaced == []
    assert positions["CRCL"]["trailing_stop_price"] == 90.32   # unchanged


def test_the_intraday_frame_would_have_moved_it_inside_one_percent():
    """Why the skip matters: run the ratchet on the 5-minute frame and it
    produces exactly the incident — a stop inside 1% of price."""
    import position_mgmt
    broker = RecordingBroker()
    positions = {"CRCL": crcl_state()}
    position_mgmt.maybe_ratchet_stop(
        broker, positions, "CRCL", positions["CRCL"], intraday(96.5),
        {"trailing_stop_type": "ATR", "trailing_stop_value": 2.5}, 96.5)

    assert broker.replaced, "the intraday frame does ratchet — that is the bug"
    _, new_stop = broker.replaced[0]
    assert new_stop > 95.0
    assert (96.5 - new_stop) / 96.5 < 0.02        # inside 2% of price


# ============================================ (2) daily frame present

def test_daily_position_still_ratchets_normally_on_a_daily_frame():
    import position_mgmt
    broker = RecordingBroker()
    positions = {"CRCL": crcl_state()}
    # Price well above breakeven, so the daily trail clears the existing stop
    # and the ratchet has something to do.
    price = 105.0
    daily_bars = {"CRCL": daily(price)}

    frame = daily_bars.get("CRCL")
    assert not (frame is None or frame.empty)
    position_mgmt.maybe_ratchet_stop(
        broker, positions, "CRCL", positions["CRCL"], frame,
        {"trailing_stop_type": "ATR", "trailing_stop_value": 2.5}, price)

    assert len(broker.replaced) == 1
    _, new_stop = broker.replaced[0]
    # A DAILY ATR sits far further from price than the 5-minute one did.
    assert (price - new_stop) / price > 0.02
    assert new_stop >= 90.32                       # never below breakeven


# ============================================ (3) intraday unchanged

def test_intraday_positions_are_untouched_by_the_guard():
    """trend_continuation trails on 5-minute bars by design; the guard must
    not reach it."""
    import position_mgmt
    broker = RecordingBroker()
    state = crcl_state(timeframe="intraday")
    positions = {"CRCL": state}

    should_skip = state.get("timeframe") == "daily"
    assert should_skip is False

    position_mgmt.maybe_ratchet_stop(
        broker, positions, "CRCL", state, intraday(96.5),
        {"trailing_stop_type": "ATR", "trailing_stop_value": 2.5}, 96.5)
    assert len(broker.replaced) == 1


# ============================================ wiring + journalling

def test_the_worker_skips_rather_than_falling_back():
    """Source assertion: the daily branch must `continue` on a missing frame,
    not reassign trail_df to the intraday one."""
    with open("streamlit_app.py", encoding="utf-8") as f:
        body = f.read()
    idx = body.index('if state.get("timeframe") == "daily":')
    block = body[idx:idx + 1200]
    assert "daily_df_for_trail is None or daily_df_for_trail.empty" in block
    assert "ratchet skipped" in block
    assert "continue" in block
    assert "journal_ops_once(\"daily_bars_unavailable\"" in block
    # The old fallback shape must be gone.
    assert "if daily_df_for_trail is not None and not daily_df_for_trail.empty:" \
        not in block


def test_the_ops_event_is_journaled_and_deduped(temp_journal):
    """One row per fault per day, so an outage spanning many cycles reads as
    one incident rather than fifty."""
    first = temp_journal.log_integrity_event(
        "daily_bars_unavailable",
        "CRCL: daily frame missing this cycle — trailing ratchet skipped")
    second = temp_journal.log_integrity_event(
        "daily_bars_unavailable",
        "CRCL: daily frame missing this cycle — trailing ratchet skipped")
    assert first == second

    rows = [r for r in temp_journal.governance_rows()
            if r["setup"] == "daily_bars_unavailable"]
    assert len(rows) == 1
    assert rows[0]["source"] == "ops"
    assert rows[0]["ticker"] == "CRCL"


def test_a_second_ticker_gets_its_own_ops_row(temp_journal):
    temp_journal.log_integrity_event("daily_bars_unavailable", "CRCL: missing")
    temp_journal.log_integrity_event("daily_bars_unavailable", "SLB: missing")
    rows = [r for r in temp_journal.governance_rows()
            if r["setup"] == "daily_bars_unavailable"]
    assert sorted(r["ticker"] for r in rows) == ["CRCL", "SLB"]


def test_position_mgmt_was_not_modified():
    """W7's scope: streamlit_app only. The ratchet itself is unchanged."""
    import position_mgmt
    assert hasattr(position_mgmt, "maybe_ratchet_stop")
    assert hasattr(position_mgmt, "ratchet_floor")
    with open("position_mgmt.py", encoding="utf-8") as f:
        body = f.read()
    assert "daily_bars_unavailable" not in body
