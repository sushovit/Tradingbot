"""
S25 — R/ATR ladder, time stop, earnings exit, and the shadow owner ladder.
"""

import datetime as dt
import json
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pandas as pd
import pytest

import earnings
import lock_replay
import position_mgmt as pm
import trade_table as tt

P = pm.R_ATR_DEFAULTS
EX = pm.DAILY_EXIT_DEFAULTS
ET = "America/New_York"
PROFILE = {"trailing_stop_type": "ATR", "trailing_stop_value": 2.5,
           "daily_trailing": "r_atr", "daily_r_atr": dict(P)}


# ================================================== ladder arithmetic (TXN)

@pytest.mark.parametrize("hc, stop, step", [
    (282.49, None, None),
    (282.50, 279.69, "+1R"),          # entry - 0.25R = 279.6875, up
    (283.625, 280.25, "+1.5R"),       # entry
    (284.75, 282.50, "+2R"),          # entry + 1R
])
def test_txn_steps(hc, stop, step):
    assert pm.r_atr_target(280.25, 278.0, hc, None, None, 1e9, P) == (stop, step)


def test_beyond_2r_the_atr_trail_takes_over_when_higher():
    # highest daily close 300, ATR 4 -> 290 > entry + 1R (282.50)
    assert pm.r_atr_target(280.25, 278.0, 300.0, 300.0, 4.0, 1e9, P) == \
        (290.0, "trail 2.5xATR")
    # ATR trail below the +2R step: the step stands
    assert pm.r_atr_target(280.25, 278.0, 290.0, 285.0, 4.0, 1e9, P)[0] == 282.5


def test_never_within_0_1pct_of_market():
    stop, step = pm.r_atr_target(280.25, 278.0, 284.75, None, None, 282.0, P)
    assert stop == 281.71 and "capped" in step


def test_initial_stop_buffer_and_clamp():
    assert pm.initial_stop(100, 97, 2.0) == 96.5          # 97 - 0.5, 1.75 ATR
    assert pm.initial_stop(100, 99.5, 2.0) == 97.0        # too tight -> 1.5 ATR
    assert pm.initial_stop(100, 90, 2.0) == 95.0          # too wide -> 2.5 ATR
    assert pm.initial_stop(100, 97, None) == 97           # no ATR: unchanged


def daily(rows, last_day="2026-10-15"):
    idx = pd.bdate_range(end=last_day, periods=len(rows))
    o, h, l, c = zip(*rows)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c}, index=idx)


def test_daily_atr_excludes_todays_partial_bar():
    rows = [(100, 102, 98, 100)] * 30 + [(100, 140, 60, 100)]   # wild partial
    df = daily(rows)
    a_with = pm.daily_atr(df, 14, None)
    a_without = pm.daily_atr(df, 14, dt.date(2026, 10, 15))
    assert a_without == pytest.approx(4.0, abs=0.01)
    assert a_with > a_without


# ================================================== live function

NOW = datetime(2026, 10, 15, 15, 0, 30, tzinfo=timezone.utc)          # 11:00 ET
EARLY = datetime(2026, 10, 15, 13, 50, 30, tzinfo=timezone.utc)       # 09:50 ET


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
    s = {"in_position": True, "source": "bot", "timeframe": "daily",
         "entry_price": 280.25, "initial_stop": 278.0,
         "trailing_stop_price": 278.0, "profit_target_price": 298.69,
         "stop_order_id": "stop-0", "shares_held": 4}
    s.update(over)
    return s


def m5(closes, now=NOW, start_et=None):
    """Completed 5-minute bars; `start_et` pins the first bar's ET time."""
    if start_et:
        first = pd.Timestamp(start_et, tz=ET).tz_convert("UTC")
        idx = [first + pd.Timedelta(minutes=5 * i) for i in range(len(closes))]
    else:
        end = pd.Timestamp(now).floor("5min") - pd.Timedelta(minutes=5)
        idx = [end - pd.Timedelta(minutes=5 * (len(closes) - 1 - i))
               for i in range(len(closes))]
    return pd.DataFrame({"open": closes, "high": closes, "low": closes,
                         "close": closes}, index=pd.DatetimeIndex(idx))


def run(state, df, last, now=NOW, daily_df=None):
    broker, journaled = FakeBroker(), []
    persisted = []
    changed = pm.maybe_pct_lock(broker, {"TXN": state}, "TXN", state, df,
                                PROFILE, last, now,
                                persist=lambda p: persisted.append(json.loads(json.dumps(p))),
                                journal_raise=lambda *a: journaled.append(a),
                                daily_df=daily_df)
    return changed, broker, journaled, persisted


def test_steps_raise_in_order_and_never_lower():
    s = txn()
    assert run(s, m5([282.6]), 282.7)[1].replaced == [("stop-0", 279.69)]
    assert run(s, m5([283.7]), 283.8)[1].replaced == [("stop-1", 280.25)]
    assert run(s, m5([284.8]), 284.9)[1].replaced == [("stop-1", 282.50)]
    changed, broker, _, _ = run(s, m5([281.0]), 281.1)          # pullback
    assert changed is False and broker.replaced == []
    assert s["trailing_stop_price"] == 282.50


def test_no_ratchet_before_10am_et():
    changed, broker, _, _ = run(txn(), m5([284.8], now=EARLY), 284.9, now=EARLY)
    assert changed is False and broker.replaced == []


def test_opening_range_closes_do_not_count():
    """A 09:35 bar closing at +2R must not trigger anything; a 10:05 bar at
    +1R does."""
    df = m5([284.8, 282.6], start_et="2026-10-15 09:35")
    df.index = pd.DatetimeIndex([pd.Timestamp("2026-10-15 09:35", tz=ET),
                                 pd.Timestamp("2026-10-15 10:05", tz=ET)]).tz_convert("UTC")
    s = txn()
    run(s, df, 282.7)
    assert s["highest_close"] == 282.6 and s["trailing_stop_price"] == 279.69


def test_restart_is_a_no_op():
    s = txn()
    _, _, _, persisted = run(s, m5([283.7]), 283.8)
    changed, broker, journaled, _ = run(persisted[-1]["TXN"], m5([283.7]), 283.8)
    assert changed is False and broker.replaced == [] and journaled == []


def test_mfe_high_is_tracked_for_the_time_stop():
    s = txn()
    df = m5([281.0])
    df["high"] = [281.9]
    run(s, df, 281.0)
    assert s["max_high"] == 281.9


# ================================================== time stop / earnings

def at(hh, mm):
    return datetime(2026, 10, 20, hh, mm)


def test_session_day_counts_the_entry_day_as_one():
    assert pm.session_day("2026-10-14", dt.date(2026, 10, 14)) == 1
    assert pm.session_day("2026-10-14", dt.date(2026, 10, 20)) == 5    # Wed..Tue


@pytest.mark.parametrize("day, mfe, when, soon, expected", [
    (5, 0.3, at(15, 55), False, "Time stop (day 5, MFE +0.30R < +0.5R)"),
    (5, 0.6, at(15, 55), False, None),             # ran far enough: keep
    (5, 0.3, at(15, 54), False, None),             # not yet the close
    (10, 3.0, at(15, 55), False, "Time stop (day 10 close)"),
    (3, 0.1, at(15, 50), True, "Earnings exit (reports within 1 session)"),
    (3, 0.1, at(15, 49), True, None),
    (3, 0.1, at(15, 50), None, None),              # calendar unknown: no exit
])
def test_daily_exit_due(day, mfe, when, soon, expected):
    assert pm.daily_exit_due(day, mfe, when, soon, EX) == expected


class FakeFinnhub:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail = rows or [], fail

    def earnings_calendar(self, _from, to, symbol):
        if self.fail:
            raise ConnectionError("down")
        return {"earningsCalendar": self.rows}


@pytest.mark.parametrize("rows, expected", [
    ([{"symbol": "TXN", "date": "2026-10-20", "hour": "amc"}], True),
    ([{"symbol": "TXN", "date": "2026-10-21", "hour": "bmo"}], True),
    ([{"symbol": "TXN", "date": "2026-10-20", "hour": "bmo"}], False),  # already out
    ([{"symbol": "TXN", "date": "2026-10-23", "hour": "amc"}], False),
    ([], False),
])
def test_reports_within_one_session(rows, expected):
    earnings.reset_cache()
    assert earnings.reports_within_one_session(
        "TXN", dt.date(2026, 10, 20), client=FakeFinnhub(rows)) is expected


def test_friday_amc_and_monday_bmo_both_count():
    earnings.reset_cache()
    rows = [{"symbol": "TXN", "date": "2026-10-19", "hour": "bmo"}]
    assert earnings.reports_within_one_session(
        "TXN", dt.date(2026, 10, 16), client=FakeFinnhub(rows)) is True


def test_unreachable_calendar_is_none():
    earnings.reset_cache()
    assert earnings.reports_within_one_session(
        "TXN", dt.date(2026, 10, 20), client=FakeFinnhub(fail=True)) is None


# ================================================== worker wiring

def src():
    with open("streamlit_app.py", encoding="utf-8") as f:
        return f.read()


def test_new_daily_entries_get_the_atr_initial_stop_before_any_gate():
    s = src()
    block = s[s.index("# S25: a NEW daily entry on the R/ATR rule"):][:1800]
    assert "position_mgmt.initial_stop(signal.entry, signal.stop" in block
    assert "signal.target = round(signal.entry + _k * (signal.entry - _new_stop), 2)" in block
    assert s.index("# S25: a NEW daily entry") < s.index("risk.check_signal(")


def test_the_worker_runs_time_and_earnings_exits_and_passes_daily_bars():
    s = src()
    assert "daily_df=daily_bars.get(ticker))" in s
    block = s[s.index("# S25: time stop and earnings exit"):][:900]
    assert "daily_time_or_earnings_exit(" in block
    assert "broker.close_position(ticker)" in block
    assert "handle_position_exit(" in block


def test_shipped_config():
    cfg = json.load(open("bot_config.json", encoding="utf-8"))
    for name in ("Aggressive", "Moderate"):
        prof = cfg["risk_profiles"][name]
        assert prof["daily_trailing"] == "r_atr"
        assert pm.r_atr_params(prof)["steps"] == [[1.0, -0.25], [1.5, 0.0], [2.0, 1.0]]
        assert pm.r_atr_params(prof)["trail_atr"] == 2.5
        assert pm.daily_exit_params(prof)["time_stop_day"] == 5
        assert pm.daily_exit_params(prof)["max_hold_days"] == 10
        assert prof["trailing_stop_type"] == "ATR"        # intraday unchanged


# ================================================== shadow owner ladder

SP = {"start_pct": 0.5, "cushion_pct": 0.5, "step_pct": 0.25}


@pytest.mark.parametrize("hc, stop", [
    (284.22, None), (284.23, 282.82),       # breakeven, rounded up
    (284.93, 282.82),                       # 283.505 < first step 283.522
    (286.00, 284.23),                       # floored to entry + 2 steps
])
def test_stepped_shadow_ladder(hc, stop):
    assert pm.stepped_ladder_target(282.815, hc, hc, SP) == stop


def bars5(rows, start="2026-10-14 13:35"):
    idx = pd.date_range(start=pd.Timestamp(start, tz="UTC"), periods=len(rows),
                        freq="5min")
    o, h, l, c = zip(*rows)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c}, index=idx)


def test_shadow_vs_live_pnl_win_and_capture():
    """Entry 100, stop 95. Price runs to 104 (best move +4) then collapses.
    Live (journal) closed at 99 (-1). The shadow ladder trails: high close
    103.9 -> 103.38 floored to 103.25, exits there (+3.25)."""
    rows = [(100, 101, 99.8, 101), (101, 104, 101, 103.9), (103.5, 103.5, 98, 98.5)]
    trade = {"ticker": "A", "qty": 1, "buy": 100.0, "buy_ts": "2026-10-14 09:30:00",
             "sell": 99.0, "sell_ts": "2026-10-14 09:55:00", "pnl": -1.0,
             "stop": 95.0, "target": 115.0}
    out = lock_replay.shadow_vs_live([trade], lambda t, s: bars5(rows), SP)
    assert out["live"]["pnl"] == -1.0 and out["live"]["win_pct"] == 0
    assert out["shadow"]["pnl"] == pytest.approx(3.25)
    assert out["shadow"]["win_pct"] == 100
    assert out["live"]["capture"] == pytest.approx(-1 / 4)
    assert out["shadow"]["capture"] == pytest.approx(3.25 / 4)


def test_weekly_live_vs_shadow_line():
    shadow = {"live": {"pnl": -1.0, "trades": 1, "wins": 0, "best": 4.0,
                       "win_pct": 0.0, "capture": -0.25},
              "shadow": {"pnl": 3.25, "trades": 1, "wins": 1, "best": 4.0,
                         "win_pct": 100.0, "capture": 0.8125}}
    lines = tt.weekly_lines([], dt.date(2026, 10, 16), 5000.0, None, shadow)
    text = "\n".join(lines)
    assert "Live vs shadow (owner ladder):" in text
    assert "Live   -$1.00  win 0%  capture -25%" in text
    assert "Shadow +$3.25  win 100%  capture 81%" in text
    assert max(len(l) for l in lines) <= 50
