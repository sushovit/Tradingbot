"""CEO 2026-10-21 - startup protection check: each open position's live stop
and target at the broker vs positions.json, repaired at once."""

from types import SimpleNamespace as N

import position_mgmt as pm


def leg(oid, kind, price):
    if kind == "stop":
        return N(id=oid, side="OrderSide.SELL", order_type="OrderType.STOP",
                 stop_price=price, limit_price=None, symbol="REGN")
    return N(id=oid, side="OrderSide.SELL", order_type="OrderType.LIMIT",
             stop_price=None, limit_price=price, symbol="REGN")


class FakeBroker:
    def __init__(self, orders):
        self.orders, self.calls = list(orders), []

    def get_live_orders(self, ticker=None):
        return list(self.orders)

    def replace_stop(self, oid, px):
        self.calls.append(("replace_stop", oid, px))
        return N(id=oid + "-r")

    def replace_target(self, oid, px):
        self.calls.append(("replace_target", oid, px))
        return N(id=oid + "-r")

    def cancel_order(self, oid):
        self.calls.append(("cancel", oid))

    def submit_oco_exit(self, ticker, qty, stop, target):
        self.calls.append(("oco", ticker, qty, stop, target))
        return N(id="oco-t", side="OrderSide.SELL", order_type="OrderType.LIMIT",
                 limit_price=target, stop_price=None,
                 legs=[leg("oco-s", "stop", stop)])


def regn(**kw):
    s = {"in_position": True, "trailing_stop_price": 756.25,
         "profit_target_price": 862.58, "stop_order_id": "s1", "target_order_id": "t1"}
    s.update(kw)
    return {"REGN": s}


def run(broker, positions):
    events, saved = [], []
    lines = pm.verify_protection(broker, positions, {"REGN": 1.0},
                                 journal_event=lambda k, d: events.append((k, d)),
                                 persist=lambda p: saved.append(1))
    return lines, events, saved


def test_matching_legs_post_the_shield_line_and_touch_nothing():
    b = FakeBroker([leg("s1", "stop", 756.25), leg("t1", "limit", 862.58)])
    lines, events, _ = run(b, regn())
    assert lines == ["🛡️ REGN stop 756.25 ✅ target 862.58 ✅"]
    assert b.calls == [] and events == []


def test_a_low_broker_stop_is_raised_to_the_record():
    b = FakeBroker([leg("s1", "stop", 750.00), leg("t1", "limit", 862.58)])
    pos = regn()
    lines, events, saved = run(b, pos)
    assert b.calls == [("replace_stop", "s1", 756.25)]
    assert lines[0].startswith("⚠️ REGN stop 756.25 🔧") and "raised 750.00 -> 756.25" in lines[0]
    assert events[0][0] == "protection_repaired" and saved
    assert pos["REGN"]["stop_order_id"] == "s1-r"


def test_a_higher_broker_stop_is_never_lowered_the_record_is_corrected():
    b = FakeBroker([leg("s1", "stop", 802.61), leg("t1", "limit", 862.58)])
    pos = regn()
    lines, events, _ = run(b, pos)
    assert b.calls == []                                  # no lowering
    assert pos["REGN"]["trailing_stop_price"] == 802.61
    assert "record corrected" in lines[0] and events


def test_a_different_target_is_moved_to_the_record():
    b = FakeBroker([leg("s1", "stop", 756.25), leg("t1", "limit", 956.21)])
    lines, events, _ = run(b, regn())
    assert b.calls == [("replace_target", "t1", 862.58)]
    assert "target moved 956.21 -> 862.58" in lines[0]


def test_a_missing_target_cancels_the_stop_and_places_a_linked_pair():
    b = FakeBroker([leg("s1", "stop", 756.25)])
    pos = regn()
    lines, events, saved = run(b, pos)
    assert b.calls == [("cancel", "s1"), ("oco", "REGN", 1.0, 756.25, 862.58)]
    assert pos["REGN"]["stop_order_id"] == "oco-s" and pos["REGN"]["target_order_id"] == "oco-t"
    assert "target missing -> re-placed as a linked pair" in lines[0]
    assert events[0][0] == "protection_repaired" and saved


def test_both_missing_places_a_linked_pair():
    b = FakeBroker([])
    lines, _, _ = run(b, regn())
    assert b.calls == [("oco", "REGN", 1.0, 756.25, 862.58)]
    assert "stop and target missing" in lines[0]


def test_no_record_to_restore_from_is_reported_not_guessed():
    b = FakeBroker([])
    lines, events, _ = run(b, regn(trailing_stop_price=None))
    assert b.calls == [] and "NOT repaired" in lines[0] and events


def test_a_broker_error_on_one_position_does_not_stop_the_check():
    class Boom(FakeBroker):
        def get_live_orders(self, ticker=None):
            raise RuntimeError("api down")
    lines, events, _ = run(Boom([]), regn())
    assert "check FAILED" in lines[0] and events


def test_positions_not_held_at_the_broker_are_skipped():
    b = FakeBroker([])
    assert pm.verify_protection(b, regn(), {}) == []


def test_it_runs_at_startup_after_the_reconcile():
    src = open("streamlit_app.py", encoding="utf-8").read()
    i_reconcile = src.index("positions = reconcile_positions(broker, positions)")
    i_refresh = src.index("refreshed = refresh_order_ids(broker, positions)")
    i_check = src.index("position_mgmt.verify_protection(")
    i_loop = src.index("    while os.path.exists(LOCK_FILE):\n        cycle_count")
    assert i_reconcile < i_refresh < i_check < i_loop
