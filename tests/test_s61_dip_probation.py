"""dip_probation (CEO-approved owner override 2026-11-01): entry, earnings block,
3-session exit, kill switch, max-1, and the worker wiring. No network."""

import datetime as dt
import json
from types import SimpleNamespace

import pandas as pd
import pytz

import dip_live as dl
import risk
import session_clock
import trade_alerts
from strategies import enabled_strategies
from strategies.base import Rejection, Signal

ET = pytz.timezone("America/New_York")
CFG = json.load(open("bot_config.json", encoding="utf-8"))
TODAY = dt.date(2026, 11, 2)                                # Monday


def daily(pc=100.0, days=25):
    idx = pd.bdate_range(end=TODAY - dt.timedelta(days=1), periods=days)
    c = [pc] * days
    return pd.DataFrame({"open": c, "high": [x + 1 for x in c], "low": [x - 1 for x in c],
                         "close": c}, index=idx)          # TR 2 -> ATR14 2.0


def m5(rows, start="09:30"):
    idx = pd.date_range(f"{TODAY} {start}", periods=len(rows), freq="5min", tz="America/New_York")
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"])


TOUCH = [(100.2, 100.4, 99.6, 99.8), (99.8, 99.9, 99.1, 99.2), (99.2, 99.3, 98.6, 98.7),
         (98.7, 98.8, 98.3, 98.4), (98.4, 98.5, 97.9, 98.0), (98.0, 98.1, 97.8, 97.9)]


def ctx(now="09:56", price=97.9, gate=None, earnings=lambda t, a, b: [], d=None, cfg=CFG):
    return {"ticker": "ABC", "config": cfg, "daily": d if d is not None else daily(),
            "now_et": ET.localize(dt.datetime.combine(TODAY, dt.time(*map(int, now.split(":"))))),
            "current_price": price, "dip_gate": gate, "earnings_dates": earnings}


# ----------------------------------------------------------------- entry

def test_first_touch_before_11_buys_next_cycle_with_the_ceo_bracket():
    s = dl.detect(m5(TOUCH), ctx())
    assert isinstance(s, Signal) and s.setup_name == "dip_probation"
    assert (s.entry, s.stop, s.target) == (97.9, 93.9, 100.0)     # 97.9 - 2 x 2.0; prior close
    assert s.extras["max_hold_sessions"] == 2 and s.extras["hard_exit_reason"] == "Time exit (session 3)"
    assert s.extras["touch_bar"].startswith("2026-11-02T09:50")


def test_no_entry_before_the_touch_after_11_stale_or_gated():
    assert dl.detect(m5(TOUCH[:4]), ctx(now="09:51")) is None            # no touch yet
    late = [(100.2, 100.4, 99.6, 99.8)] * 18 + [(98.0, 98.1, 97.0, 97.5)]  # touch bar 11:00
    assert dl.detect(m5(late), ctx(now="11:06")) is None
    assert dl.detect(m5(TOUCH + [(98, 98.2, 97.9, 98.1)] * 3), ctx(now="10:11")) is None   # stale
    assert dl.detect(m5(TOUCH), ctx(gate="dip_position_open")) is None


def test_universe_and_open_band_filters():
    assert dl.detect(m5(TOUCH), ctx(d=daily(pc=45.0), price=43.9)) is None   # no touch at 45 x 0.98
    low = [(45.0, 45.1, 44.6, 44.7)] * 4 + [(44.6, 44.7, 44.0, 44.1), (44.1, 44.2, 44.0, 44.1)]
    r = dl.detect(m5(low), ctx(d=daily(pc=45.0), price=44.1))
    assert isinstance(r, Rejection) and r.filter_name == "dip_price_floor"
    gap = [(101.5, 101.6, 100.5, 100.8)] + TOUCH[1:]          # opens +1.5%, touches at 09:50
    r = dl.detect(m5(gap), ctx())
    assert isinstance(r, Rejection) and r.filter_name == "dip_open_outside_band"


# ----------------------------------------------------------------- earnings block

def test_earnings_within_3_sessions_blocks_and_unknown_fails_closed():
    r = dl.detect(m5(TOUCH), ctx(earnings=lambda t, a, b: ["2026-11-05"]))
    assert isinstance(r, Rejection) and r.filter_name == "dip_earnings_ahead"
    r = dl.detect(m5(TOUCH), ctx(earnings=lambda t, a, b: None))
    assert isinstance(r, Rejection) and r.filter_name == "dip_earnings_unknown"
    assert isinstance(dl.detect(m5(TOUCH), ctx(earnings=lambda t, a, b: ["2026-11-10"])), Signal)


# ----------------------------------------------------------------- 3-session exit

def test_time_exit_is_1555_on_session_3():
    assert session_clock.sessions_forward_date(TODAY, 2) == "2026-11-04"        # Mon -> Wed
    assert session_clock.sessions_forward_date(dt.date(2026, 11, 5), 2) == "2026-11-09"
    src = open("streamlit_app.py", encoding="utf-8").read()
    assert '"hard_exit_reason": signal.extras.get("hard_exit_reason"),' in src
    assert ('exit_px, state.get("hard_exit_reason")\n'
            '                                                 or "Hard Exit Date", now_et,') in src
    i = src.index('hard_exit = state.get("hard_exit_date")')
    assert "dttime(15, 55)" in src[i:i + 600]


# ----------------------------------------------------------------- kill switch

def test_kill_switch_at_minus_75_or_5_straight_losses():
    assert dl.kill_state([-25, -25, -25])[0] is True
    assert dl.kill_state([-10] * 5)[:3] == (True, -50.0, 5)
    assert dl.kill_state([-10, -10, -10, -10, 5])[0] is False
    assert dl.kill_state([-30, -30, 10])[:2] == (False, -50.0)
    assert dl.kill_state([5, -10, -10, -10, -10, -10])[2] == 5


def test_kill_alert_is_sent_once(monkeypatch):
    import alerts
    import streamlit_app as app
    sent = []
    monkeypatch.setattr(alerts, "post_async", lambda m, *a, **k: sent.append(m))
    assert app.dip_kill_alert_once((True, -76.0, 2, "cumulative P&L $-76.00 <= $-75")) is True
    assert app.dip_kill_alert_once((True, -76.0, 2, "x")) is False
    assert len(sent) == 1 and sent[0].startswith("🧪 DIP probation KILLED")


# ----------------------------------------------------------------- max-1

def test_max_open_max_one_entry_a_day_and_start_date():
    # CEO 2026-11-03: the dip book allows 3 open (separate from the main book)
    open_pos = {t: {"in_position": True, "setup": "dip_probation"} for t in ("ABC", "DEF", "GHI")}
    other = {"XYZ": {"in_position": True, "setup": "momentum_continuation"}}
    assert dl.cycle_gate(TODAY, open_pos, 0, [], CFG) == "dip_position_open"
    assert dl.cycle_gate(TODAY, other, 1, [], CFG) == "dip_entry_done_today"
    assert dl.cycle_gate(TODAY, other, 0, [], CFG) is None
    assert dl.cycle_gate(dt.date(2026, 10, 30), {}, 0, [], CFG) == "not_started"
    assert dl.cycle_gate(TODAY, {}, 0, [-30, -30, -20], CFG).startswith("killed")


# ----------------------------------------------------------------- wiring

def test_registered_for_every_ticker_while_enabled():
    names = [s.name for s in enabled_strategies("ANY", CFG)]
    assert "dip_probation" in names and "pullback_in_uptrend" not in names
    assert "dip_probation" in [s.name for s in enabled_strategies("NVDA", CFG)]
    off = dict(CFG, dip_probation=dict(CFG["dip_probation"], enabled=False))
    assert "dip_probation" not in [s.name for s in enabled_strategies("ANY", off)]


def test_reward_risk_override_only_for_the_dip():
    assert risk.check_signal(97.9, 93.9, 100.0, 5000)[0] is False          # R:R 0.5 < 1.5
    assert risk.check_signal(97.9, 93.9, 100.0, 5000, min_reward_risk=0.0) == (True, None)
    assert risk.check_signal(97.9, 93.9, 97.0, 5000, min_reward_risk=0.0)[0] is False


def test_worker_wiring_rules_only_risk_and_no_trailing():
    src = open("streamlit_app.py", encoding="utf-8").read()
    assert "is_dip = signal.setup_name == dip_live.DIP" in src
    assert 'decision_context["advisory"] = True' in src
    assert "if is_dip:\n                        # Rules only" in src
    assert 'entry_risk_pct = min(float(dip_live.params(config)["risk_per_trade_pct"]),' in src
    assert "min_lot_tolerance=0.0 if _dip_entry else lot_tolerance" in src
    assert src.count("min_reward_risk=(0.0 if signal.setup_name == dip_live.DIP else None)") == 2
    i, j = src.index("if state.get(\"setup\") == dip_live.DIP:"), src.index("# 4) Trailing stop:")
    assert i < j and "continue" in src[i:j]                 # no ladder / trail / floor
    assert 'or _st.get("setup") == dip_live.DIP):      # dip target = prior close' in src
    assert "not dip_live.params(config)[\"spy_gate\"]" in src
    assert CFG["dip_probation"]["risk_per_trade_pct"] == 0.5 and CFG["dip_probation"]["spy_gate"] is False


def test_dip_alerts_say_dip():
    e = trade_alerts.buy_embed(ticker="ABC", qty=6, fill=97.9, ts=ET.localize(dt.datetime(2026, 11, 2, 9, 56)),
                               target=100.0, stop=93.9, setup="dip_probation", conviction=None)
    assert e["title"] == "🧪 DIP 🟢 BUY ABC"
    assert "if state.get(\"setup\") == \"dip_probation\"" in open("trade_alerts.py", encoding="utf-8").read()


def test_stop_is_reanchored_once_to_fill_minus_2_atr(monkeypatch):
    import streamlit_app as app
    calls = []
    broker = SimpleNamespace(replace_stop=lambda oid, px: calls.append((oid, px)) or SimpleNamespace(id="s2"))
    monkeypatch.setattr(app, "write_positions", lambda p: None)
    st = {"setup": "dip_probation", "entry_fill_confirmed": True, "entry_price": 97.5, "atr14": 2.0,
          "trailing_stop_price": 93.9, "stop_order_id": "s1"}
    pos = {"ABC": st}
    assert app.dip_reanchor_once(broker, pos, "ABC", st, 98.0) is True
    assert calls == [("s1", 93.5)] and st["trailing_stop_price"] == 93.5 and st["stop_order_id"] == "s2"
    assert app.dip_reanchor_once(broker, pos, "ABC", st, 98.0) is False and len(calls) == 1
    pending = {"setup": "dip_probation", "entry_fill_confirmed": False}
    assert app.dip_reanchor_once(broker, {"X": pending}, "X", pending, 98.0) is False


def test_dip_reported_separately():
    rows = [{"ticker": "ABC", "buy_ts": "2026-11-02 09:56:10", "buy": 97.9, "sell": 100.0,
             "sell_ts": "2026-11-02 13:00:00", "pnl": 12.6, "reason": "Profit Target", "setup": "dip_probation"}]
    lines = dl.summary_lines(rows, CFG)
    assert lines[0].startswith("🧪 DIP probation (LIVE, rules only, $25 risk): 1 trade(s), 1 closed, 1 won")
    import scoreboard
    assert scoreboard.table_lines([], {"live": 0, "rule": 0, "owner": 0, "closed_n": 0, "closed_diff": 0,
                                       "unscored": [], "rule_met": False}, None, None, lines)[-len(lines) - 4:]


def test_a_dip_entry_closes_the_gate_for_the_rest_of_the_cycle():
    """2026-11-02: ABBV, PSX and ZS were bought in one cycle - the per-cycle
    gate must close as soon as a dip entry is made."""
    src = open("streamlit_app.py", encoding="utf-8").read()
    i = src.index('status_updates.append(f"{ticker}: BOUGHT ({signal.setup_name})")')
    after = src[i:i + 500]
    assert "if signal.setup_name == dip_live.DIP:" in after
    assert 'dip_gate = "dip_entry_done_today"' in after
    # and the next ticker's detector sees it
    assert '"dip_gate": dip_gate}' in src
    assert dl.detect(m5(TOUCH), ctx(gate="dip_entry_done_today")) is None
