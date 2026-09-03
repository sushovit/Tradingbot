"""
SPY regime on completed daily bars (2026-09-02). No network.

The bug this locks down: the live filter used a 20-period EMA of FIVE-MINUTE
bars (~100 minutes) while the backtest that justified the filter used the
20-DAY EMA of daily closes. The desk enforced a regime nobody had measured.
These tests pin the corrected definition, its completed-bar semantics, the
journal evidence format, and the position timeframe that the same cycle
writes.
"""

import json
import re

import pandas as pd
import pytest

import backtest
import daily_eval
import regime_audit


def spy_frame(closes, start="2026-07-01"):
    idx = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({"open": closes, "high": closes, "low": closes,
                         "close": closes,
                         "volume": [1_000_000] * len(closes)}, index=idx)


# ============================================ definition matches the backtest

def test_regime_matches_backtest_spy_regime_series():
    """The whole point: live and backtest must compute the SAME indicator."""
    closes = [100 + i * 0.5 for i in range(40)]
    df = spy_frame(closes)
    live = daily_eval.spy_regime(df, today_str="2099-01-01")   # no partial bar
    series = backtest.spy_regime_series(df)
    assert live["trending"] is bool(series.iloc[-1])
    assert live["spy_close"] == pytest.approx(float(df["close"].iloc[-1]))
    assert live["ema20d"] == pytest.approx(
        float(df["close"].ewm(span=20, adjust=False).mean().iloc[-1]))


def test_regime_uses_a_twenty_DAY_span_not_twenty_bars_of_intraday():
    """A 20-period EMA of 5-min bars is a ~100-minute average. Guard the span
    so nobody silently reintroduces the old indicator."""
    assert daily_eval.SPY_EMA_SPAN == 20
    closes = [100.0] * 30 + [130.0]
    df = spy_frame(closes)
    r = daily_eval.spy_regime(df, today_str="2099-01-01")
    # One 30-point day cannot drag a 20-DAY EMA all the way up.
    assert r["ema20d"] < 105.0
    assert r["trending"] is True


# ============================================ completed-bar semantics

def test_todays_partial_bar_is_never_used():
    closes = [100 + i for i in range(25)]
    df = spy_frame(closes, start="2026-08-03")
    today = str(df.index[-1])[:10]
    r = daily_eval.spy_regime(df, today_str=today)
    assert r["as_of"] == str(df.index[-2])[:10]
    assert r["spy_close"] == pytest.approx(float(df["close"].iloc[-2]))


def test_a_partial_bar_cannot_flip_the_regime():
    """An intraday plunge on today's forming bar must not move the gate."""
    closes = [100 + i * 0.5 for i in range(30)]
    df = spy_frame(closes, start="2026-07-20")
    before = daily_eval.spy_regime(df.iloc[:-1], today_str="2099-01-01")
    crashed = df.copy()
    crashed.iloc[-1, crashed.columns.get_loc("close")] = 1.0   # today, partial
    after = daily_eval.spy_regime(crashed, today_str=str(df.index[-1])[:10])
    assert before["trending"] == after["trending"] is True
    assert after["spy_close"] == before["spy_close"]


def test_regime_returns_none_when_it_cannot_decide():
    """None means UNKNOWN. It must never be mistaken for 'chop'."""
    assert daily_eval.spy_regime(None) is None
    assert daily_eval.spy_regime(spy_frame([100.0])) is None
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    assert daily_eval.spy_regime(empty) is None


def test_thin_history_reads_as_unknown_not_as_a_verdict():
    """A 20-day EMA on fewer than 20 completed closes is a shape, not a
    regime. A short IEX fetch must fail closed rather than gate every entry
    on an average that has not filled up yet."""
    nineteen = spy_frame([100 + i * 0.5 for i in range(19)])
    assert daily_eval.spy_regime(nineteen, today_str="2099-01-01") is None

    twenty = spy_frame([100 + i * 0.5 for i in range(20)])
    verdict = daily_eval.spy_regime(twenty, today_str="2099-01-01")
    assert verdict is not None
    assert verdict["trending"] is True
    assert verdict["as_of"] == str(twenty.index[-1])[:10]

    # The partial bar is dropped BEFORE the count, so 20 rows whose last is
    # today leaves only 19 completed closes — still unknown.
    with_partial = spy_frame([100 + i * 0.5 for i in range(20)])
    today = str(with_partial.index[-1])[:10]
    assert daily_eval.spy_regime(with_partial, today_str=today) is None


def test_chop_is_detected_below_the_ema():
    closes = [200 - i * 2 for i in range(30)]
    r = daily_eval.spy_regime(spy_frame(closes), today_str="2099-01-01")
    assert r["trending"] is False
    assert r["spy_close"] < r["ema20d"]


# ============================================ journal evidence format

REGIME_RE = re.compile(
    r"spy_close=(\d+\.\d{2}) ema20d=(\d+\.\d{2}) "
    r"regime=(trending|chop) as_of=(\d{4}-\d{2}-\d{2})")


def test_regime_details_format_is_exactly_the_ratified_string():
    closes = [100 + i * 0.5 for i in range(30)]
    r = daily_eval.spy_regime(spy_frame(closes), today_str="2099-01-01")
    text = daily_eval.regime_details(r)
    m = REGIME_RE.fullmatch(text)
    assert m, text
    assert float(m.group(1)) == pytest.approx(r["spy_close"], abs=0.005)
    assert float(m.group(2)) == pytest.approx(r["ema20d"], abs=0.005)
    assert m.group(3) == "trending"
    assert m.group(4) == r["as_of"]


def test_regime_details_says_unknown_rather_than_lying():
    text = daily_eval.regime_details(None)
    assert "regime=unknown" in text
    assert "regime=chop" not in text and "regime=trending" not in text


def test_worker_appends_regime_evidence_to_both_gated_rows():
    """Item 1's contract: EVERY spy_bearish pass and EVERY chop_reclaim tag
    carries the evidence. Asserted on the source because both rows are
    written from inside the live cycle."""
    with open("streamlit_app.py", encoding="utf-8") as f:
        body = f.read()
    # The evidence string is built once per cycle...
    assert "spy_evidence = daily_eval.regime_details(spy_regime)" in body
    # ...and appended to both regime-gated journal rows.
    bearish = body[body.index('"spy_bearish"'):]
    assert "{spy_evidence}" in bearish[:400]
    chop = body[body.index('"chop_reclaim",\n'):]
    assert "{spy_evidence}" in chop[:600]


def test_worker_no_longer_reads_five_minute_spy_bars():
    with open("streamlit_app.py", encoding="utf-8") as f:
        body = f.read()
    assert 'get_bars(["SPY"]' not in body        # the 5-minute call is gone
    assert 'get_daily_bars(["SPY"]' in body
    assert "lookback_days=60" in body
    # ...fetched once per session, not once per cycle.
    assert 'spy_regime_cache.get("fetched_on") != now_et.date()' in body


def test_journaled_details_round_trip_through_the_journal(temp_journal):
    """The details string must survive journalling intact — the audit reads
    it back out of the context column."""
    closes = [100 + i * 0.5 for i in range(30)]
    regime = daily_eval.spy_regime(spy_frame(closes), today_str="2099-01-01")
    evidence = daily_eval.regime_details(regime)
    temp_journal.log_rules_pass("NVDA", "trend_continuation", "spy_bearish",
                                f"SPY below its 20-day EMA {evidence}")

    rows = regime_audit.load_rejections(temp_journal.DB_FILE)
    assert len(rows) == 1
    assert REGIME_RE.search(rows[0]["details"]), rows[0]["details"]
    assert f"as_of={regime['as_of']}" in rows[0]["details"]


# ============================================ item 2: position timeframe

def test_position_timeframe_comes_from_the_resolved_strategy_timeframe():
    """It used to be re-derived from a hardcoded pair of setup names, so any
    setup added later silently became 'intraday' and trailed a daily
    structure with 5-min ATR — what killed NOK."""
    with open("streamlit_app.py", encoding="utf-8") as f:
        body = f.read()
    assert '"timeframe": signal_timeframe,' in body
    assert "signal_timeframe = timeframe" in body
    assert '("mean_reversion_reclaim", "momentum_continuation")' not in body


def test_every_daily_setup_resolves_to_daily():
    """The setups the old hardcoding would have mistagged."""
    from strategies import REGISTRY
    with open("bot_config.json", encoding="utf-8") as f:
        config = json.load(f)
    for name in ("pullback_in_uptrend", "post_earnings_continuation",
                 "mean_reversion_reclaim", "momentum_continuation"):
        cls = REGISTRY[name]
        assert daily_eval.strategy_timeframe(name, config,
                                             cls.timeframe) == "daily"
    assert daily_eval.strategy_timeframe(
        "trend_continuation", config,
        REGISTRY["trend_continuation"].timeframe) == "intraday"


def test_config_override_still_wins_over_the_class_default():
    cfg = {"strategy_timeframes": {"trend_continuation": "daily"}}
    assert daily_eval.strategy_timeframe(
        "trend_continuation", cfg, "intraday") == "daily"


# ============================================ item 3: the audit

def test_audit_flips_only_rows_that_were_actually_trending():
    rejections = [{"date": "2026-07-16", "ticker": "CSCO",
                   "setup": "trend_continuation", "details": ""},
                  {"date": "2026-07-17", "ticker": "MRK",
                   "setup": "mean_reversion_reclaim", "details": ""}]
    regimes = {
        "2026-07-16": {"trending": True, "spy_close": 754.77,
                       "ema20d": 746.75, "as_of": "2026-07-15"},
        "2026-07-17": {"trending": False, "spy_close": 740.0,
                       "ema20d": 747.14, "as_of": "2026-07-16"},
    }
    result = regime_audit.audit(rejections, [], regimes)
    assert len(result["flipped"]) == 1
    assert result["flipped"][0]["ticker"] == "CSCO"
    assert result["upheld"] == 1
    assert result["flip_pct"] == 50.0


def test_audit_counts_unknown_dates_separately_from_upheld():
    """A date with no SPY history is INDETERMINATE, not a upheld block."""
    rejections = [{"date": "2026-01-01", "ticker": "X", "setup": "s",
                   "details": ""}]
    result = regime_audit.audit(rejections, [], {"2026-01-01": None})
    assert result["unknown"] == 1
    assert result["upheld"] == 0 and result["flipped"] == []


def test_audit_flags_chop_exemptions_taken_in_trending_markets():
    tags = [{"date": "2026-07-16", "ticker": "UBER", "setup": "mrr"},
            {"date": "2026-07-17", "ticker": "ORCL", "setup": "mrr"}]
    regimes = {"2026-07-16": {"trending": True, "spy_close": 1.0,
                              "ema20d": 0.5, "as_of": "2026-07-15"},
               "2026-07-17": {"trending": False, "spy_close": 0.5,
                              "ema20d": 1.0, "as_of": "2026-07-16"}}
    result = regime_audit.audit([], tags, regimes)
    assert result["chop_tags"] == 2
    assert result["chop_tags_actually_trending"] == 1


def test_audit_report_states_the_upper_bound_caveat():
    """Required by the work order: flipped rejections are an UPPER BOUND on
    lost entries, and the report must say so."""
    result = regime_audit.audit([], [], {})
    text = regime_audit.render(result, None)
    assert "UPPER BOUND" in text
    assert "gatekeeper" in text and "max-positions" in text


def test_audit_regime_lookup_never_uses_a_future_bar():
    """A decision on date D may only see sessions completed before D."""
    # Needs >= 20 completed closes BEFORE the target date, or the lookup is
    # (correctly) indeterminate under the thin-history guard.
    closes = [100 + i for i in range(60)]
    spy = spy_frame(closes, start="2026-06-01")
    target = str(spy.index[40])[:10]
    regimes = regime_audit.spy_regime_by_date(spy, [target])
    assert regimes[target]["as_of"] < target
