"""
S28 target cap (CEO, 2026-10-16): target = fill + min(3R, 3.0 x ATR14),
applied to the live target leg after the fill.
"""

import json
import os

import position_mgmt as pm

ROOT = os.path.join(os.path.dirname(__file__), "..")


class Order:
    def __init__(self, id):
        self.id = id


class Broker:
    def __init__(self):
        self.replaced = []

    def replace_target(self, order_id, price):
        self.replaced.append((order_id, price))
        return Order(f"tgt-{len(self.replaced)}")


def regn():
    return {"in_position": True, "source": "bot", "timeframe": "daily",
            "entry_price": 802.61, "initial_stop": 756.25,
            "original_stop": 756.25, "profit_target_price": 956.21,
            "target_order_id": "tgt-0", "shares_held": 1}


def test_config_ships_the_cap():
    cfg = json.load(open(os.path.join(ROOT, "bot_config.json"), encoding="utf-8"))
    # CEO 2026-10-27 (agenda 37, S56): 3.0 -> 1.5 x ATR14 for new entries
    assert cfg["exits"]["target_cap_atr"] == 1.5
    assert pm.target_cap_atr(cfg) == 1.5 and pm.target_cap_atr({}) is None


def test_wide_r_is_capped_regn():
    # R 46.36 = 2.32 ATR; 3R = 139.08 vs 3 x 19.99 = 59.97 -> the ATR cap
    assert pm.capped_target(802.61, 756.25, 19.99, 3.0) == 862.58


def test_tight_r_is_unchanged():
    # R 1.0 on an ATR of 4.0: 3R = 3.0 < 3 ATR = 12.0
    assert pm.capped_target(100.0, 99.0, 4.0, 3.0) == 103.0
    assert pm.capped_target(100.0, 99.0, None, 3.0) == 103.0      # no ATR


def test_regn_leg_is_moved_once_and_recorded():
    s, b, events, alerts, saves = regn(), Broker(), [], [], []
    assert pm.maybe_cap_target(b, "REGN", s, 19.99, 3.0, persist=lambda: saves.append(1),
                               journal_event=lambda k, d: events.append((k, d)),
                               alert=alerts.append) is True
    assert b.replaced == [("tgt-0", 862.58)]
    assert s["profit_target_price"] == 862.58 and s["uncapped_target"] == 956.21
    assert s["target_order_id"] == "tgt-1" and s["target_capped"] is True
    assert events[0][0] == "target_capped" and "956.21 -> 862.58" in events[0][1]
    assert alerts and "Target capped" in alerts[0] and saves
    # idempotent: a second pass never touches the broker again
    assert pm.maybe_cap_target(b, "REGN", s, 19.99, 3.0) is False
    assert len(b.replaced) == 1


def test_tight_r_position_is_left_alone_and_marked():
    s = regn()
    s.update(entry_price=100.0, initial_stop=99.0, original_stop=99.0,
             profit_target_price=103.0)
    b = Broker()
    assert pm.maybe_cap_target(b, "X", s, 4.0, 3.0) is False
    assert b.replaced == [] and s["target_capped"] is False
    assert s["profit_target_price"] == 103.0


def test_never_raises_a_target_and_needs_atr():
    s = regn(); s["profit_target_price"] = 850.0     # already below the cap
    b = Broker()
    assert pm.maybe_cap_target(b, "REGN", s, 19.99, 3.0) is False and b.replaced == []
    s2 = regn()
    assert pm.maybe_cap_target(Broker(), "REGN", s2, None, 3.0) is False
    assert "target_capped" not in s2                 # retried once ATR is known


def test_ceo_positions_are_never_capped():
    s = regn(); s["source"] = "ceo"
    b = Broker()
    assert pm.maybe_cap_target(b, "REGN", s, 19.99, 3.0) is False and b.replaced == []


def test_the_cap_runs_after_the_fill_and_never_before_the_entry_gates():
    src = open(os.path.join(ROOT, "streamlit_app.py"), encoding="utf-8").read()
    loop = src[src.index("def _worker_loop():"):]
    cap = loop.index("position_mgmt.maybe_cap_target(")
    assert loop.index("reconcile_entry_with_broker(") < cap < loop.index("maybe_pct_lock(")
    # the entry gates still see the setup's own target (reward:risk >= 1.5)
    assert "capped_target(" not in loop[:loop.index("risk.check_signal(")]


def test_a_cap_failure_never_costs_the_cycle():
    src = open(os.path.join(ROOT, "streamlit_app.py"), encoding="utf-8").read()
    block = src[src.index("position_mgmt.maybe_cap_target("):]
    assert block.index("except Exception as e:") < block.index("status_updates = []")


def test_startup_repair_never_points_at_a_leg_being_replaced():
    import streamlit_app as app

    class L:
        def __init__(self, id, status, otype, replaced_by=None):
            self.id, self.status, self.order_type = id, status, otype
            self.side, self.replaced_by = "OrderSide.SELL", replaced_by

    class B:
        def get_live_orders(self, t):
            return [L("new-tgt", "OrderStatus.NEW", "OrderType.LIMIT"),
                    L("old-tgt", "OrderStatus.PENDING_REPLACE", "OrderType.LIMIT",
                      replaced_by="new-tgt"),
                    L("stop", "OrderStatus.HELD", "OrderType.STOP")]
    book = {"REGN": {"in_position": True, "target_order_id": "new-tgt",
                     "stop_order_id": "stop"}}
    assert app.refresh_order_ids(B(), book) == []
    assert book["REGN"]["target_order_id"] == "new-tgt"
