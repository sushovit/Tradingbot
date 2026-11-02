"""2026-11-02 safety fix: dip_probation opened ABBV, PSX and ZS in one cycle
despite max 1 open / 1 entry a day. End to end through the real worker loop
(fake broker): two dip signals in the same cycle, in consecutive cycles, and a
pending dip order the journal never saw -> never more than one order."""

from types import SimpleNamespace as N

import pandas as pd

import dip_live
import universe
import tests.test_s19b_async_discord as harness
from tests.test_s19b_async_discord import _run_worker
from strategies.base import Signal


class FakeDip:
    """A dip_probation signal on every ticker, honouring the cycle gate
    exactly like dip_live.detect does."""
    name = "dip_probation"
    timeframe = "intraday"

    def detect(self, df, context):
        if context.get("dip_gate"):
            return None
        e = float(df["close"].iloc[-1])
        return Signal(setup_name="dip_probation", ticker=context["ticker"], entry=e,
                      stop=round(e * 0.97, 2), target=round(e * 1.01, 2),
                      extras={"atr14": e * 0.015, "prior_close": e * 1.01,
                              "max_hold_sessions": 2,
                              "hard_exit_reason": dip_live.EXIT_REASON})


def drive(app, monkeypatch, tmp_path, cycles, broker_orders=None):
    import analyst
    import daily_eval
    submitted, orders = [], list(broker_orders or [])
    monkeypatch.setattr(app, "enabled_strategies", lambda t, c: [FakeDip()])
    monkeypatch.setattr(daily_eval, "gap_abort", lambda df, px: (False, 0, 0))
    monkeypatch.setattr(universe, "watch_only_today", lambda: {})
    monkeypatch.setattr(universe, "refresh", lambda *a, **k: ["ABBV", "ZS"])  # two sectors
    monkeypatch.setattr(app, "earnings_gap_state_for", lambda *a, **k: ((None,) * 4, []))
    monkeypatch.setattr(app, "gatekeeper_event_context", lambda *a, **k: ("", []))
    real = dip_live.params
    monkeypatch.setattr(dip_live, "params", lambda cfg=None: dict(real(cfg), start="2026-01-01"))
    monkeypatch.setattr(analyst, "get_verdict", lambda *a, **k: (
        {"approved": False, "conviction_score": 10, "reasoning": "advisory"}, None))

    def bars(self, tickers, timeframe_minutes=5, lookback_days=5):
        idx = pd.date_range("2026-10-14 13:30", periods=40, freq="5min", tz="UTC")
        px = [300.0 + 0.01 * i for i in range(40)]
        df = pd.DataFrame({"open": px, "high": px, "low": px, "close": px, "volume": 1000},
                          index=idx)
        return {t: df for t in tickers}
    monkeypatch.setattr(harness.WorkerBroker, "get_bars", bars)
    monkeypatch.setattr(harness.WorkerBroker, "get_latest_price", lambda self, t: 300.4,
                        raising=False)

    def submit(self, *a, **k):
        submitted.append((a, k))
        o = N(id=f"order-{a[0]}", client_order_id=k.get("client_order_id"), side="OrderSide.BUY",
              status="OrderStatus.NEW", legs=[])
        orders.append(o)
        return o
    monkeypatch.setattr(harness.WorkerBroker, "submit_bracket", submit, raising=False)
    monkeypatch.setattr(harness.WorkerBroker, "orders_since", lambda self, after: orders,
                        raising=False)
    for name, fn in (("get_closed_orders_since", lambda self, *a, **k: []),
                     ("get_open_orders", lambda self, *a, **k: []),
                     ("replace_stop", lambda self, *a, **k: N(id="stop-2")),
                     ("replace_target", lambda self, *a, **k: N(id="tgt-2"))):
        monkeypatch.setattr(harness.WorkerBroker, name, fn, raising=False)
    monkeypatch.setattr(harness.WorkerBroker, "get_order", lambda self, oid: N(
        id=oid, status="OrderStatus.NEW", filled_avg_price=None, filled_qty="0",
        legs=[], symbol="X"), raising=False)          # entries stay PENDING
    _run_worker(app, monkeypatch, tmp_path, cycles=cycles)
    return submitted


def test_two_dip_signals_in_the_same_cycle_make_one_order(temp_journal, tmp_path, monkeypatch):
    import streamlit_app as app
    sent = drive(app, monkeypatch, tmp_path, cycles=1)
    assert len(sent) == 1
    assert sent[0][1]["client_order_id"].startswith("dip-")


def test_dip_signals_in_consecutive_cycles_make_one_order(temp_journal, tmp_path, monkeypatch):
    import streamlit_app as app
    sent = drive(app, monkeypatch, tmp_path, cycles=3)
    assert len(sent) == 1                    # the pending first entry blocks every later one


def test_a_pending_dip_order_the_journal_never_saw_blocks(temp_journal, tmp_path, monkeypatch):
    import streamlit_app as app
    pending = N(id="o-9", client_order_id="dip-PSX-20261014095500", side="OrderSide.BUY",
                status="OrderStatus.ACCEPTED", legs=[])
    assert drive(app, monkeypatch, tmp_path, cycles=2, broker_orders=[pending]) == []


def test_presubmit_block_rules():
    o = lambda coid, st, side="OrderSide.BUY": N(client_order_id=coid, status=st, side=side)
    cfg = {"dip_probation": {"max_open": 1, "max_entries_per_day": 1}}
    assert dip_live.presubmit_block({}, 0, [], cfg) is None
    assert dip_live.presubmit_block({}, 0, None, cfg) == "dip_check_unavailable"
    assert dip_live.presubmit_block({"A": {"in_position": True, "setup": "dip_probation"}}, 0, [],
                                    cfg) == "dip_position_open"
    assert dip_live.presubmit_block({}, 0, [o("dip-A-1", "OrderStatus.NEW")], cfg) == "dip_order_pending"
    assert dip_live.presubmit_block({}, 1, [], cfg) == "dip_entry_done_today"
    assert dip_live.presubmit_block({}, 0, [o("dip-A-1", "OrderStatus.FILLED")], cfg) == \
        "dip_entry_done_today"
    assert dip_live.presubmit_block({}, 0, [o("dip-A-1", "OrderStatus.CANCELED"),
                                            o("x-B", "OrderStatus.NEW")], cfg) is None
