"""CEO 2026-10-31 (agenda 39): event-window sizing, earnings-gap cooldown,
gatekeeper event context, SPY gate post. No network."""

import datetime
import json

import pandas as pd

import event_risk as er
import prompts

CFG = json.load(open("bot_config.json", encoding="utf-8"))
D = datetime.date


def daily(rows, start="2026-10-01"):
    """rows = [(open, close)] on consecutive business days."""
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame({"open": [o for o, _ in rows], "close": [c for _, c in rows]}, index=idx)


# ----------------------------------------------------------------- 1. sizing

def test_event_window_halves_risk_on_the_ceo_dates_only():
    assert er.event_window_risk_pct(1.0, CFG, D(2026, 10, 30)) == 1.0
    for d in (D(2026, 11, 2), D(2026, 11, 3), D(2026, 11, 13)):
        assert er.event_window_risk_pct(1.0, CFG, d) == 0.5
    assert er.event_window_risk_pct(1.0, CFG, D(2026, 11, 16)) == 1.0
    assert er.event_window_risk_pct(0.25, CFG, D(2026, 11, 3)) == 0.25     # never raises risk


def test_worker_sizes_with_the_event_window():
    src = open("streamlit_app.py", encoding="utf-8").read()
    assert ("base_risk_pct = event_risk.event_window_risk_pct(\n"
            "                risk_profile['risk_per_trade_pct'], config, now_et.date())") in src


# ----------------------------------------------------------------- 2. cooldown

GAP_ROWS = [(100, 100)] * 5 + [(91.5, 92)] + [(92, 93)] * 12    # -8.5% gap on session 6


def test_earnings_gap_blocks_3_sessions_then_reclaim_only():
    d = daily(GAP_ROWS)
    gap_day = d.index[5].date()
    earn = [d.index[4].date().isoformat()]                       # reported the evening before
    for k, want in ((5, "cooldown"), (7, "cooldown"), (8, "reclaim_only"), (14, "reclaim_only"),
                    (15, None)):
        st = er.earnings_gap_state(d, earn, d.index[k].date(), CFG)
        assert st[0] == want, (k, st)
    st = er.earnings_gap_state(d, earn, d.index[6].date(), CFG)
    assert st[1] == gap_day and st[2] == -8.5 and st[3] == 2
    assert er.earnings_gap_block("momentum_continuation", ("cooldown",)) == "earnings_gap_cooldown"
    assert er.earnings_gap_block("mean_reversion_reclaim", ("cooldown",)) == "earnings_gap_cooldown"
    assert er.earnings_gap_block("momentum_continuation", ("reclaim_only",)) == "earnings_gap_reclaim_only"
    assert er.earnings_gap_block("mean_reversion_reclaim", ("reclaim_only",)) is None


def test_a_gap_without_earnings_does_not_count_unknown_calendar_does():
    d = daily(GAP_ROWS)
    today = d.index[6].date()
    assert er.earnings_gap_state(d, [], today, CFG)[0] is None          # not an earnings gap
    assert er.earnings_gap_state(d, None, today, CFG)[0] == "cooldown"   # unknown: safe side
    small = daily([(100, 100)] * 5 + [(93, 93)] + [(93, 93)] * 3)       # -7%: no block
    assert er.earnings_gap_state(small, None, small.index[6].date(), CFG)[0] is None


# ----------------------------------------------------------------- 3. context

def test_groups_gap_sympathy_and_earnings_ahead():
    d = daily([(100, 100)] * 5 + [(96, 97)])                          # -4% gap today
    today = d.index[5].date()
    assert er.stock_groups(d, today, past_dates=[d.index[4].date().isoformat()]) == ["gap"]
    assert er.stock_groups(d, today, past_dates=[]) == ["sympathy"]
    flat = daily([(100, 100)] * 6)
    ahead = (today + datetime.timedelta(days=7)).isoformat()
    assert er.stock_groups(flat, today, [], [ahead], hold_sessions=10) == ["earnings-ahead"]
    assert er.stock_groups(flat, today, [], [ahead], hold_sessions=0) == []


def test_events_inside_the_hold():
    ev = er.events_in_hold(CFG, D(2026, 11, 2), 10)
    assert [d.isoformat() for d, _ in ev] == ["2026-11-03", "2026-11-06", "2026-11-10", "2026-11-13"]
    assert [d.isoformat() for d, _ in er.events_in_hold(CFG, D(2026, 11, 9), 0)] == []
    assert [d.isoformat() for d, _ in er.events_in_hold(CFG, D(2026, 11, 10), 0)] == ["2026-11-10"]
    txt = er.context_text(["earnings-ahead"], ev, "2026-11-05", "Event-window sizing active")
    assert "Group: earnings-ahead" in txt and "Tue 11-03 US midterm elections" in txt
    assert "Own earnings inside the hold: 2026-11-05" in txt


def test_prompt_carries_the_context_for_both_models():
    p = prompts.build_gatekeeper_user_prompt("X", "c", 20, 50, 1, "up", 1, 2, 10, 9, 13, 3, 5, 9,
                                             21, "n", event_context="- Group: gap")
    assert "=== EVENT CONTEXT (informational - not a rule change) ===\n- Group: gap" in p
    for f in ("claude_integration.py", "local_analyst.py"):
        assert "event_context=event_context" in open(f, encoding="utf-8").read(), f
    src = open("streamlit_app.py", encoding="utf-8").read()
    assert 'gk_kwargs["event_context"] = _ev_ctx' in src
    assert '"groups": _ev_groups, "event_context": _ev_ctx,' in src


# ----------------------------------------------------------------- 4. SPY gate

def test_spy_gate_line_and_once_per_session_post():
    import streamlit_app as app
    now = datetime.datetime(2026, 11, 2, 9, 31)
    up = {"trending": True, "spy_close": 600.0, "ema20d": 590.0, "as_of": "2026-10-30"}
    down = dict(up, trending=False, spy_close=580.0)
    assert app.spy_gate_line(up, now).startswith("🧭 SPY 20-day EMA gate Mon 11-02 09:31 ET: OPEN")
    assert "CLOSED - SPY below its 20-day EMA: momentum entries blocked" in app.spy_gate_line(down, now)
    src = open("streamlit_app.py", encoding="utf-8").read()
    assert "now_et.date() not in spy_gate_posted" in src and "(now_et.hour, now_et.minute) >= (9, 30)" in src
