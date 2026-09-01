"""
Work order 2026-09-02 items 2, 3 and 5: the two ratified setups running in
the LIVE pipeline. No network — the earnings calendar is injected.

The load-bearing tests here are the two that cost money if wrong: the
earnings gate must FAIL CLOSED, and probation must cap concurrency.
"""

import datetime
import json

import pandas as pd
import pytest

import earnings
import risk
import session_clock
from strategies import REGISTRY
from strategies.base import Signal, Rejection
from strategies.post_earnings_continuation import PostEarningsContinuation
from strategies.pullback_in_uptrend import PullbackInUptrend

CFG = {"setup_probation": {"go_live_date": "2026-09-02", "trades": 20,
                           "max_concurrent": 1,
                           "setups": ["pullback_in_uptrend",
                                      "post_earnings_continuation"]}}


class FakeFinnhub:
    """Stands in for the Finnhub client. `rows` is the calendar payload;
    `boom` makes every call raise, which is the fail-closed case."""

    def __init__(self, rows=None, boom=False):
        self.rows = rows or []
        self.boom = boom
        self.calls = 0

    def earnings_calendar(self, _from=None, to=None, symbol=""):
        self.calls += 1
        if self.boom:
            raise RuntimeError("finnhub 403")
        return {"earningsCalendar": self.rows}


@pytest.fixture(autouse=True)
def _clear_calendar():
    earnings.reset_cache()
    yield
    earnings.reset_cache()


def frame(rows, start="2026-01-01"):
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close",
                                       "volume"], index=idx)


# ============================================================ earnings gate

def test_calendar_hit_and_miss():
    client = FakeFinnhub([{"symbol": "NVDA", "date": "2026-08-31"}])
    asof = datetime.date(2026, 9, 1)
    assert earnings.had_earnings_within("NVDA", 3, asof, client=client) is True
    assert earnings.had_earnings_within("AMD", 3, asof, client=client) is False


def test_event_outside_the_window_does_not_count():
    client = FakeFinnhub([{"symbol": "NVDA", "date": "2026-08-10"}])
    asof = datetime.date(2026, 9, 1)
    assert earnings.had_earnings_within("NVDA", 3, asof, client=client) is False


def test_gate_fails_closed_when_the_calendar_is_unreachable():
    """The whole point of the gate: no calendar, no trade. It must return
    None (unknown), never False-as-if-checked and never True."""
    client = FakeFinnhub(boom=True)
    got = earnings.had_earnings_within("NVDA", 3, datetime.date(2026, 9, 1),
                                       client=client)
    assert got is None


def test_calendar_is_fetched_once_per_day_not_once_per_ticker():
    client = FakeFinnhub([{"symbol": "NVDA", "date": "2026-08-31"}])
    asof = datetime.date(2026, 9, 1)
    for sym in ("NVDA", "AMD", "INTC", "MU"):
        earnings.had_earnings_within(sym, 3, asof, client=client)
    assert client.calls == 1


def test_failed_fetch_is_not_retried_every_cycle():
    client = FakeFinnhub(boom=True)
    asof = datetime.date(2026, 9, 1)
    for _ in range(5):
        earnings.had_earnings_within("NVDA", 3, asof, client=client)
    assert client.calls == 1        # one failure, then cached as failed


def test_sessions_back_skips_weekends():
    # 2026-09-01 is a Tuesday; 3 sessions back is the previous Thursday.
    assert earnings.sessions_back(datetime.date(2026, 9, 1), 3) == \
        datetime.date(2026, 8, 27)


# ================================================== post_earnings_continuation

def pec_frame(gap_pct=8.0, gap_volume=40000, bars_after=2, close_above=True):
    rows = []
    price = 50.0
    for _ in range(40):
        rows.append((price, price * 1.005, price * 0.995, price, 10000))
    prev_close = price
    gap_open = prev_close * (1 + gap_pct / 100)
    gap_high, gap_low = gap_open * 1.02, gap_open * 0.99
    rows.append((gap_open, gap_high, gap_low, gap_open * 1.01, gap_volume))
    for k in range(bars_after):
        last = k == bars_after - 1
        close = gap_high * (1.01 if (last and close_above) else 0.99)
        rows.append((gap_high, max(close, gap_high) * 1.005, gap_low * 1.001,
                     close, 15000))
    rows.append((rows[-1][3], rows[-1][3] * 1.01, rows[-1][3] * 0.99,
                 rows[-1][3], 12000))                 # forming bar
    return frame(rows), gap_low


def _pec_context(monkeypatch, verdict):
    monkeypatch.setattr(earnings, "had_earnings_within",
                        lambda *a, **k: verdict)
    return {"ticker": "NVDA", "config": CFG,
            "risk_profile": {"risk_per_trade_pct": 1.0}}


def test_pec_fires_when_an_earnings_event_is_confirmed(monkeypatch):
    import strategies.post_earnings_continuation as mod
    monkeypatch.setattr(mod.earnings_calendar, "had_earnings_within",
                        lambda *a, **k: True)
    df, gap_low = pec_frame()
    result = PostEarningsContinuation().detect(df, _pec_context(monkeypatch, True))
    assert isinstance(result, Signal), result
    assert result.stop == pytest.approx(gap_low)
    r_dist = result.entry - result.stop
    assert result.target == pytest.approx(result.entry + 4.0 * r_dist)
    assert result.extras["max_hold_sessions"] == 55


def test_pec_refuses_a_gap_with_no_earnings_event(monkeypatch):
    """This is the difference between the earnings class and the generic
    gap-up class the backtest could only measure."""
    import strategies.post_earnings_continuation as mod
    monkeypatch.setattr(mod.earnings_calendar, "had_earnings_within",
                        lambda *a, **k: False)
    df, _ = pec_frame()
    result = PostEarningsContinuation().detect(df, _pec_context(monkeypatch, False))
    assert isinstance(result, Rejection)
    assert result.filter_name == "no_earnings_event"


def test_pec_fails_closed_when_the_calendar_is_down(monkeypatch):
    """Calendar unavailable must SKIP and journal, never trade the bare gap."""
    import strategies.post_earnings_continuation as mod
    monkeypatch.setattr(mod.earnings_calendar, "had_earnings_within",
                        lambda *a, **k: None)
    df, _ = pec_frame()
    result = PostEarningsContinuation().detect(df, _pec_context(monkeypatch, None))
    assert isinstance(result, Rejection)
    assert result.filter_name == "earnings_calendar_unavailable"


def test_pec_ignores_small_gaps_and_thin_volume(monkeypatch):
    import strategies.post_earnings_continuation as mod
    monkeypatch.setattr(mod.earnings_calendar, "had_earnings_within",
                        lambda *a, **k: True)
    ctx = _pec_context(monkeypatch, True)
    small, _ = pec_frame(gap_pct=2.0)
    assert PostEarningsContinuation().detect(small, ctx) is None
    thin, _ = pec_frame(gap_volume=11000)
    assert PostEarningsContinuation().detect(thin, ctx) is None


def test_pec_window_closes_after_five_sessions(monkeypatch):
    import strategies.post_earnings_continuation as mod
    monkeypatch.setattr(mod.earnings_calendar, "had_earnings_within",
                        lambda *a, **k: True)
    df, _ = pec_frame(bars_after=7)
    assert PostEarningsContinuation().detect(
        df, _pec_context(monkeypatch, True)) is None


def test_pec_never_calls_the_calendar_before_the_gap_qualifies(monkeypatch):
    """Cost + correctness: no earnings lookup for a chart with no gap."""
    import strategies.post_earnings_continuation as mod
    calls = []
    monkeypatch.setattr(mod.earnings_calendar, "had_earnings_within",
                        lambda *a, **k: calls.append(a) or True)
    flat = frame([(50, 50.2, 49.8, 50, 10000) for _ in range(60)])
    PostEarningsContinuation().detect(flat, _pec_context(monkeypatch, True))
    assert calls == []


# ======================================================= pullback_in_uptrend

def pullback_frame(pullback_volume=500):
    rows = []
    price = 100.0
    for _ in range(55):
        price *= 1.012
        rows.append((price * 0.995, price * 1.01, price * 0.99, price, 2000))
    peak = price
    for k in range(6):
        price = peak * (1 - 0.08 * (k + 1) / 6)
        rows.append((price * 1.004, price * 1.006, price * 0.994, price,
                     pullback_volume))
    low_bar = price
    for _ in range(6):
        price *= 1.004
        rows.append((price * 0.998, price * 1.03, price * 0.996, price, 900))
    prior_high = rows[-1][1]
    close = prior_high * 1.02
    rows.append((close * 0.99, close * 1.005, close * 0.985, close, 3000))
    rows.append((close, close * 1.01, close * 0.99, close, 2500))
    return frame(rows), low_bar


PB_CTX = {"ticker": "AMD", "config": CFG,
          "risk_profile": {"risk_per_trade_pct": 1.0}}


def test_pullback_fires_at_4r_with_the_stop_under_the_pullback_low():
    df, low_bar = pullback_frame()
    result = PullbackInUptrend().detect(df, PB_CTX)
    assert isinstance(result, Signal), result
    assert result.stop <= low_bar * 1.001
    r_dist = result.entry - result.stop
    assert result.target == pytest.approx(result.entry + 4.0 * r_dist)


def test_pullback_rejects_distribution_volume():
    df, _ = pullback_frame(pullback_volume=9000)
    result = PullbackInUptrend().detect(df, PB_CTX)
    assert isinstance(result, Rejection)
    assert result.filter_name == "pullback_volume_not_declining"


def test_pullback_rejects_a_downtrend():
    rows = []
    price = 300.0
    for _ in range(70):
        price *= 0.99
        rows.append((price * 1.005, price * 1.01, price * 0.99, price, 1000))
    assert PullbackInUptrend().detect(frame(rows), PB_CTX) is None


# ============================================================ probation

def test_probation_does_not_touch_established_setups():
    assert risk.setup_risk_pct("momentum_continuation", 0, 1.0, CFG) == 1.0
    assert risk.on_probation("momentum_continuation", 0, CFG) is False


def test_unknown_trade_count_stays_cautious():
    assert risk.on_probation("pullback_in_uptrend", None, CFG) is True


def test_live_entry_count_ignores_ceo_orders(temp_journal):
    temp_journal.log_trade("AMD", "BUY", 1, 100.0, reason="pullback_in_uptrend")
    temp_journal.log_trade("AMD", "BUY", 1, 100.0,
                           reason="CEO pullback_in_uptrend")
    temp_journal.log_trade("AMD", "SELL", 1, 110.0, reason="pullback_in_uptrend")
    assert temp_journal.live_entry_count("pullback_in_uptrend") == 1


# ============================================================ hold cap

def test_hold_cap_lands_inside_a_quarter():
    got = session_clock.sessions_forward_date(datetime.date(2026, 9, 2), 55)
    assert got == "2026-11-18"
    delta = datetime.date.fromisoformat(got) - datetime.date(2026, 9, 2)
    assert delta.days < 92                    # inside one quarter
    assert session_clock.sessions_forward_date(datetime.date(2026, 9, 2),
                                               None) is None


def test_hold_cap_skips_weekends():
    # Friday + 1 session = Monday.
    assert session_clock.sessions_forward_date(datetime.date(2026, 9, 4), 1) \
        == "2026-09-07"


# ================================================ probation revision (2026-09-02)
# The original half-risk rule was WITHDRAWN: a 0.5% budget on a $2,000 cap is
# $10, so any setup with a stop wider than $10/share sized to zero. Exposure
# is now bounded by concurrency instead.

def test_probation_no_longer_discounts_size():
    for n in (0, 5, 19, 20):
        assert risk.setup_risk_pct("pullback_in_uptrend", n, 1.0, CFG) == 1.0
    assert risk.setup_risk_pct("post_earnings_continuation", 0, 0.75, CFG) == 0.75


def test_probation_allows_one_open_position_per_setup():
    ok, reason = risk.check_setup_probation("pullback_in_uptrend", 0, 0, CFG)
    assert ok and reason is None
    ok, reason = risk.check_setup_probation("pullback_in_uptrend", 1, 0, CFG)
    assert not ok and reason == "probation_position_open"


def test_probation_slots_are_per_setup_not_shared():
    """One open pullback must not block a post-earnings entry."""
    ok, _ = risk.check_setup_probation("post_earnings_continuation", 0, 0, CFG)
    assert ok


def test_graduated_setup_has_no_concurrency_cap():
    ok, _ = risk.check_setup_probation("pullback_in_uptrend", 3, 20, CFG)
    assert ok
    ok, _ = risk.check_setup_probation("momentum_continuation", 5, 0, CFG)
    assert ok


def test_unknown_open_count_does_not_crash_the_gate():
    ok, _ = risk.check_setup_probation("pullback_in_uptrend", None, 0, CFG)
    assert ok is True          # unparseable -> treated as zero open


def test_full_risk_doubles_the_sizeable_stop_band_but_does_not_remove_the_wall():
    """What the revision actually buys, measured — not assumed.

    Going 0.5% -> 1% doubles the risk budget on a $2,000 cap from $10 to $20,
    so stops between $10 and $20 a share become sizeable. It does NOT rescue
    the wide-stop names that prompted the change: CRM's $26.07 stop and
    INTU's $36.27 stop are unsizeable at BOTH rates. That wall is the capital
    cap, which is why item 2 (size_zero reporting) is the part that will
    actually tell us what we are losing."""
    equity, cap = 2000.0, 0.25

    # Newly sizeable: a $15 stop was zero at half risk, is one share at full.
    assert risk.position_size(equity, 0.5, 100.0, 85.0,
                              position_cap_pct=cap) == 0
    assert risk.position_size(equity, 1.0, 100.0, 85.0,
                              position_cap_pct=cap) == 1

    # Still unsizeable: the two live signals that triggered the revision.
    for entry, stop in ((257.63, 231.56), (359.27, 323.00)):
        assert risk.position_size(equity, 0.5, entry, stop,
                                  position_cap_pct=cap) == 0
        assert risk.position_size(equity, 1.0, entry, stop,
                                  position_cap_pct=cap) == 0


# ================================================ size_zero reporting (item 2)

def test_size_zero_report_carries_stop_distance_and_budget(temp_journal):
    temp_journal.log_rules_pass(
        "CRM", "post_earnings_continuation", "size_zero",
        "entry=257.63 stop=231.56 stop_distance_usd=26.07 "
        "risk_budget_usd=20.00 affordable_shares=0.77 risk_pct=1.0 "
        "equity=2000.00")
    rows = temp_journal.size_zero_report(temp_journal._now_et()[:7])
    assert len(rows) == 1
    r = rows[0]
    assert r["setup"] == "post_earnings_continuation"
    assert r["size_zero"] == 1
    assert r["median_stop_usd"] == 26.07
    assert r["median_budget_usd"] == 20.00
    assert r["rate_pct"] == 100.0      # one blocked, none taken


def test_size_zero_rate_is_blocked_over_blocked_plus_taken(temp_journal):
    month = temp_journal._now_et()[:7]
    temp_journal.log_rules_pass("CRM", "post_earnings_continuation",
                                "size_zero", "stop_distance_usd=26.07 "
                                "risk_budget_usd=20.00")
    for t in ("AMZN", "JPM", "MSFT"):
        temp_journal.log_trade(t, "BUY", 1, 100.0,
                               reason="post_earnings_continuation")
    rows = temp_journal.size_zero_report(month)
    assert rows[0]["entries"] == 3
    assert rows[0]["rate_pct"] == 25.0        # 1 of 4 qualifying signals


def test_size_zero_report_ignores_other_filters(temp_journal):
    temp_journal.log_rules_pass("NVDA", "pullback_in_uptrend", "volume_low", "x")
    assert temp_journal.size_zero_report(temp_journal._now_et()[:7]) == []


# ================================================ probation grade lines

def test_probation_trades_are_numbered_and_carry_outcomes(temp_journal):
    temp_journal.log_trade("AMZN", "BUY", 1, 100.0,
                           reason="pullback_in_uptrend")
    temp_journal.record_exit("AMZN", 1, 90.0, "stop", entry_price=100.0,
                             broker_order_id="o1")
    temp_journal.log_trade("JPM", "BUY", 2, 50.0, reason="pullback_in_uptrend")

    rows = temp_journal.probation_trades("pullback_in_uptrend", 20)
    assert [r["n"] for r in rows] == [1, 2]
    assert rows[0]["closed"] and rows[0]["pnl_usd"] == -10.0
    assert rows[1]["closed"] is False and rows[1]["pnl_usd"] is None


def test_probation_trade_list_stops_at_the_limit(temp_journal):
    for i in range(25):
        temp_journal.log_trade(f"T{i:02d}", "BUY", 1, 10.0,
                               reason="pullback_in_uptrend")
    assert len(temp_journal.probation_trades("pullback_in_uptrend", 20)) == 20


def test_review_bot_demands_a_line_per_probation_trade():
    import review_bot
    prompt = review_bot.REVIEW_SYSTEM_PROMPT
    assert "GRADE EVERY PROBATION TRADE" in prompt
    assert "its OWN line" in prompt
    # Graded on the DECISION, not the outcome — otherwise 20 trades of noise
    # teach us nothing about the setup.
    assert "not the outcome" in prompt
    assert "PROBATION TRADES: none yet" in prompt


def test_review_bot_bundles_the_probation_trades(temp_journal, monkeypatch):
    import review_bot
    monkeypatch.setattr(review_bot, "journal", temp_journal)
    temp_journal.log_trade("AMZN", "BUY", 1, 100.0,
                           reason="pullback_in_uptrend")
    text = review_bot.probation_lines()
    assert "pullback_in_uptrend #1/20" in text
    assert "AMZN" in text and "still OPEN" in text
