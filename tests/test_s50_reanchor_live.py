"""S50 adopted (CEO 2026-10-23): a new daily R/ATR position's initial stop is
re-anchored to the broker fill once - same ATR clamp, same shares."""

import json
from types import SimpleNamespace as N

import position_mgmt as pm

PROFILE = {"daily_trailing": "r_atr"}


def kmi(**kw):
    s = {"in_position": True, "timeframe": "daily", "entry_fill_confirmed": True,
         "entry_price": 34.01, "signal_price": 34.295, "structural_stop": 33.40,
         "atr14": 0.60, "initial_stop": 33.21, "original_stop": 33.21,
         "trailing_stop_price": 33.21, "stop_order_id": "s1", "shares_held": 50}
    s.update(kw)
    return s


class B:
    def __init__(self):
        self.calls = []

    def replace_stop(self, oid, px):
        self.calls.append((oid, px))
        return N(id="s2")


def test_kmi_stop_moves_to_the_fill_anchored_level_once():
    st, b, ev = kmi(), B(), []
    want = pm.initial_stop(34.01, 33.40, 0.60, pm.r_atr_params(PROFILE))
    assert pm.maybe_reanchor_stop(b, "KMI", st, PROFILE, last_price=34.0,
                                  journal_event=lambda k, d: ev.append(k))
    assert b.calls == [("s1", want)] and st["stop_order_id"] == "s2"
    assert st["initial_stop"] == st["original_stop"] == st["trailing_stop_price"] == want
    assert st["stop_signal_anchored"] == 33.21 and st["stop_reanchored"] is True
    assert ev == ["stop_reanchored"]
    assert not pm.maybe_reanchor_stop(b, "KMI", st, PROFILE, 34.0)      # once only
    assert len(b.calls) == 1
    assert st["shares_held"] == 50                                     # same shares


def test_never_after_the_ladder_raised_the_stop():
    st, b = kmi(trailing_stop_price=34.01), B()
    assert not pm.maybe_reanchor_stop(b, "KMI", st, PROFILE, 35.0)
    assert b.calls == [] and st["stop_reanchored"] is True


def test_waits_for_a_confirmed_fill_and_needs_the_structural_low():
    b = B()
    st = kmi(entry_fill_confirmed=False)
    assert not pm.maybe_reanchor_stop(b, "KMI", st, PROFILE, 34.0)
    assert "stop_reanchored" not in st                    # decided later
    st = kmi(structural_stop=None)
    assert not pm.maybe_reanchor_stop(b, "KMI", st, PROFILE, 34.0) and b.calls == []


def test_never_above_the_market_and_never_for_intraday_or_other_rules():
    b = B()
    assert pm.reanchor_target(kmi(), PROFILE, last_price=32.0) is None
    assert pm.reanchor_target(kmi(timeframe="intraday"), PROFILE, 34.0) is None
    assert pm.reanchor_target(kmi(), {"daily_trailing": "ladder"}, 34.0) is None


def test_shipped_config_and_worker_order():
    cfg = json.load(open("bot_config.json", encoding="utf-8"))
    assert cfg["exits"]["reanchor_stop_to_fill"] is True
    src = open("streamlit_app.py", encoding="utf-8").read()
    i_rec = src.index("_what = reconcile_entry_with_broker(")
    i_re = src.index("position_mgmt.maybe_reanchor_stop(")
    i_cap = src.index("position_mgmt.maybe_cap_target(")
    assert i_rec < i_re < i_cap
    assert '"structural_stop": signal.extras.get("structural_stop", signal.stop)' in src
