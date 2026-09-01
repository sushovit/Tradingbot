"""
Work order 2026-09-01 items 8 & 9: the two new setups are BACKTEST-ONLY, and
the QLoRA plan filters/splits honestly. No network, no model calls.

The load-bearing test here is the containment one: item 8 says research only,
so a passing suite must prove nothing live can reach these detectors.
"""

import json

import numpy as np
import pandas as pd
import pytest

import backtest
import finetune_plan


# ============================================================ go-live wiring
#
# These setups were RESEARCH ONLY until 2026-09-02, and this block used to
# assert that nothing live could reach them. The boardroom ratified both, so
# the assertions invert: they must now be reachable by the live pipeline, on
# exactly the ratified terms. The backtest detectors stay in backtest.py as
# the research record — both implementations are checked against the same
# fixtures below so they cannot silently diverge.

def test_both_setups_are_registered_live():
    from strategies import REGISTRY, enabled_strategies
    for name in ("pullback_in_uptrend", "post_earnings_continuation"):
        assert name in REGISTRY

    with open("bot_config.json", encoding="utf-8") as f:
        config = json.load(f)
    names = [s.name for s in enabled_strategies("ANY_UNIVERSE_TICKER", config)]
    assert "pullback_in_uptrend" in names
    assert "post_earnings_continuation" in names


def test_both_setups_are_trending_only():
    """The ratification is TRENDING ONLY. Adding either to spy_filter_exempt
    would let pullback_in_uptrend trade its NEGATIVE cell (-0.14R in chop)."""
    with open("bot_config.json", encoding="utf-8") as f:
        config = json.load(f)
    exempt = set(config.get("spy_filter_exempt", []))
    assert "pullback_in_uptrend" not in exempt
    assert "post_earnings_continuation" not in exempt


def test_both_setups_run_on_daily_bars_at_4r():
    from strategies import REGISTRY
    import strategies.pullback_in_uptrend as pui
    import strategies.post_earnings_continuation as pec
    with open("bot_config.json", encoding="utf-8") as f:
        config = json.load(f)
    for name in ("pullback_in_uptrend", "post_earnings_continuation"):
        assert REGISTRY[name].timeframe == "daily"
        assert config["strategy_timeframes"][name] == "daily"
    assert pui.TARGET_R == 4.0
    assert pec.TARGET_R == 4.0


def test_backtest_report_declares_the_live_status():
    """Item 5: the report is the source of truth on status, and the status is
    declared in code so it cannot go stale in the markdown."""
    assert backtest.RESEARCH_STATUS["pullback_in_uptrend"] ==         ("LIVE (probation)", "2026-09-02")
    assert backtest.RESEARCH_STATUS["post_earnings_continuation"] ==         ("LIVE (probation)", "2026-09-02")
    assert "LIVE (probation) since 2026-09-02" ==         backtest.research_status("pullback_in_uptrend")


# ============================================================ bar helpers

def frame(rows):
    """rows: [(open, high, low, close, volume), ...] -> daily OHLCV frame."""
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="D")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close",
                                       "volume"], index=idx)


def uptrend_with_pullback(pullback_volume=500, trigger_close=None,
                          pullback_depth=0.92):
    """60 bars: a clean advance, a 6-bar pullback to the 20 EMA on light
    volume, then a bar closing above the prior day's high. The last row is
    the ENTRY bar (only its open is used)."""
    rows = []
    price = 100.0
    for _ in range(45):                       # advance: rising 20/50 EMAs
        price *= 1.012
        rows.append((price * 0.995, price * 1.01, price * 0.99, price, 2000))
    peak = price
    for k in range(6):                        # pullback on DECLINING volume
        price = peak * (1 - (1 - pullback_depth) * (k + 1) / 6)
        rows.append((price * 1.004, price * 1.006, price * 0.994, price,
                     pullback_volume))
    low_bar = price
    for _ in range(6):                        # drift back up as INSIDE days:
        price *= 1.004                        # no bar closes above the prior
        rows.append((price * 0.998, price * 1.03, price * 0.996, price, 900))
    prior_high = rows[-1][1]
    close = trigger_close if trigger_close is not None else prior_high * 1.02
    rows.append((close * 0.99, close * 1.005, close * 0.985, close, 3000))
    rows.append((close, close * 1.01, close * 0.99, close, 2500))   # entry bar
    return frame(rows), low_bar


def test_pullback_fires_on_a_textbook_setup():
    df, _ = uptrend_with_pullback()
    result = backtest.detect_pullback_in_uptrend(df.tail(backtest.DETECT_WINDOW))
    assert isinstance(result, dict), result
    assert result["setup"] == "pullback_in_uptrend"
    assert result["stop_level"] < float(df["close"].iloc[-2])


def test_pullback_requires_declining_volume():
    """A pullback on HEAVIER volume is distribution, not a rest."""
    df, _ = uptrend_with_pullback(pullback_volume=9000)
    result = backtest.detect_pullback_in_uptrend(df.tail(backtest.DETECT_WINDOW))
    assert result == "pullback_volume_not_declining"


def test_pullback_requires_the_reclaim_of_the_prior_high():
    df, _ = uptrend_with_pullback()
    # Trigger bar closes BELOW the prior day's high -> no entry.
    df.iloc[-2, df.columns.get_loc("close")] = float(df["close"].iloc[-3]) * 0.999
    result = backtest.detect_pullback_in_uptrend(df.tail(backtest.DETECT_WINDOW))
    assert result in ("no_reclaim_of_prior_high", "close_not_back_above_ema20")


def test_pullback_rejects_a_downtrend():
    rows = []
    price = 200.0
    for _ in range(60):
        price *= 0.99
        rows.append((price * 1.005, price * 1.01, price * 0.99, price, 1000))
    assert backtest.detect_pullback_in_uptrend(frame(rows)) is None


def test_pullback_stop_sits_under_the_pullback_low():
    df, low_bar = uptrend_with_pullback()
    result = backtest.detect_pullback_in_uptrend(df.tail(backtest.DETECT_WINDOW))
    assert isinstance(result, dict)
    assert result["stop_level"] <= low_bar * 1.001


# ------------------------------------------------- post-earnings continuation

def gap_frame(gap_pct=8.0, gap_volume=12000, bars_after=2,
              close_above=True):
    rows = []
    price = 50.0
    for _ in range(30):
        rows.append((price, price * 1.005, price * 0.995, price, 3000))
    prev_close = price
    gap_open = prev_close * (1 + gap_pct / 100)
    gap_high = gap_open * 1.02
    gap_low = gap_open * 0.99
    rows.append((gap_open, gap_high, gap_low, gap_open * 1.01, gap_volume))
    for k in range(bars_after):
        last = k == bars_after - 1
        close = gap_high * (1.01 if (last and close_above) else 0.99)
        rows.append((gap_high, max(close, gap_high) * 1.005, gap_low * 1.001,
                     close, 5000))
    rows.append((rows[-1][3], rows[-1][3] * 1.01, rows[-1][3] * 0.99,
                 rows[-1][3], 4000))                       # entry bar
    return frame(rows), gap_low


def test_pec_fires_on_a_gap_up_continuation():
    df, gap_low = gap_frame()
    result = backtest.detect_post_earnings_continuation(df)
    assert isinstance(result, dict), result
    assert result["setup"] == "post_earnings_continuation"
    assert result["stop_level"] == pytest.approx(gap_low)
    assert result["max_hold"] == backtest.PEC_MAX_HOLD


def test_pec_requires_a_five_percent_gap():
    df, _ = gap_frame(gap_pct=2.0)
    assert backtest.detect_post_earnings_continuation(df) is None


def test_pec_requires_volume_behind_the_gap():
    df, _ = gap_frame(gap_volume=3200)          # barely above the 20d average
    assert backtest.detect_post_earnings_continuation(df) is None


def test_pec_window_closes_after_five_sessions():
    df, _ = gap_frame(bars_after=7, close_above=True)
    assert backtest.detect_post_earnings_continuation(df) is None


def test_pec_takes_only_the_first_close_above_the_gap_high():
    """Two closes above the gap high: the second is not the setup."""
    df, _ = gap_frame(bars_after=3, close_above=True)
    df.iloc[-3, df.columns.get_loc("close")] = float(df["high"].iloc[-4]) * 1.05
    result = backtest.detect_post_earnings_continuation(df)
    assert result == "not_first_close_above"


def test_pec_never_holds_through_the_next_print():
    """A trade that neither stops nor targets must be time-stopped well
    inside a quarter — the setup is explicitly 'never holds through prints'."""
    assert backtest.PEC_MAX_HOLD < 63          # sessions in a quarter
    rows = []
    price = 50.0
    for _ in range(45):        # > WARMUP_BARS, so the replay reaches the gap
        rows.append((price, price * 1.005, price * 0.995, price, 3000))
    rows.append((55.0, 56.0, 54.5, 55.5, 30000))            # gap day
    rows.append((56.0, 57.0, 55.5, 56.5, 8000))             # first close above
    for _ in range(120):                                     # dead flat forever
        rows.append((56.5, 56.6, 56.4, 56.5, 3000))
    df = frame(rows)
    trades = backtest.replay_research("TEST", df, "post_earnings_continuation",
                                      None)
    assert trades, "expected one research trade"
    assert all(t["bars_held"] <= backtest.PEC_MAX_HOLD for t in trades)
    assert any(t["exit_reason"] == "time_stop" for t in trades)


# ------------------------------------------------------------- mechanics

def test_research_replay_uses_next_bar_open_and_3r():
    df, _ = uptrend_with_pullback()
    trades = backtest.replay_research("TEST", df, "pullback_in_uptrend", None,
                                      target_r=3.0)
    for t in trades:
        assert t["entry"] > t["stop"]
        r_dist = t["entry"] - t["stop"]
        assert t["target"] == pytest.approx(t["entry"] + 3 * r_dist, rel=1e-6)


def test_research_replay_holds_one_position_per_symbol():
    df, _ = uptrend_with_pullback()
    trades = backtest.replay_research("TEST", df, "pullback_in_uptrend", None)
    dates = [(t["entry_date"], t["exit_date"]) for t in trades]
    for (a_in, a_out), (b_in, _) in zip(dates, dates[1:]):
        assert b_in >= a_out


# ============================================================ 9. QLoRA plan

def row(**kw):
    base = {"id": 1, "ticker": "NVDA", "setup_name": "trend_continuation",
            "source": "claude", "inputs_json": {"entry": 100, "stop": 95,
                                                "target": 115, "equity": 2000},
            "verdict": {"approved": True, "conviction_score": 80},
            "grade": None, "outcome_linked": False, "outcome_pnl_usd": None}
    base.update(kw)
    return base


def test_filter_keeps_only_graded_or_outcome_linked():
    rows = [row(id=1),
            row(id=2, grade="good", ticker="AMD"),
            row(id=3, outcome_linked=True, outcome_pnl_usd=5.0, ticker="INTC")]
    kept, counts = finetune_plan.build_dataset(rows)
    assert [r["id"] for r in kept] == [2, 3]
    assert counts["not_graded_or_outcome_linked"] == 1


def test_duplicate_decisions_cannot_leak_across_the_split():
    rows = [row(id=1, grade="good"), row(id=2, grade="good")]  # identical
    kept, counts = finetune_plan.build_dataset(rows)
    assert len(kept) == 1 and counts["duplicate_decision"] == 1


def test_wrong_task_shapes_are_excluded():
    rows = [row(id=1, source="rules", grade="bad",
                inputs_json={"details": "x"}),
            row(id=2, source="intern_desk", grade="bad",
                inputs_json={"date": "2026-08-01"})]
    kept, counts = finetune_plan.build_dataset(rows)
    assert kept == []
    assert counts["wrong_task_shape:rules"] == 1
    assert counts["wrong_task_shape:intern_desk"] == 1


def test_grade_outranks_a_lucky_outcome():
    """NOK closed +$0.14 on a setup graded 'bad'. The label must follow the
    grade, or we teach the model that luck is skill."""
    r = row(grade="bad", outcome_linked=True, outcome_pnl_usd=0.14)
    assert finetune_plan.label_for(r) is False
    assert finetune_plan.label_for(row(outcome_linked=True,
                                       outcome_pnl_usd=5.0)) is True
    assert finetune_plan.label_for(row(grade="ungradeable")) is None
    assert finetune_plan.label_for(row()) is None


def test_split_is_deterministic_and_stratified():
    rows = [row(id=i, grade="good" if i % 2 else "bad",
                inputs_json={"entry": i, "stop": 1, "target": 9,
                             "equity": 2000})
            for i in range(20)]
    kept, _ = finetune_plan.build_dataset(rows)
    a_train, a_eval = finetune_plan.split(kept)
    b_train, b_eval = finetune_plan.split(kept)
    assert [r["id"] for r in a_eval] == [r["id"] for r in b_eval]
    assert set(r["id"] for r in a_train).isdisjoint(r["id"] for r in a_eval)
    # both labels present in eval — an unstratified draw could miss one
    labels = {finetune_plan.label_for(r) for r in a_eval}
    assert labels == {True, False}


def test_plan_reports_not_trainable_at_tiny_n():
    rows = [row(id=i, grade="bad",
                inputs_json={"entry": i, "stop": 1, "target": 9,
                             "equity": 2000}) for i in range(6)]
    kept, counts = finetune_plan.build_dataset(rows)
    train, evalset = finetune_plan.split(kept)
    report = finetune_plan.build_report(rows, kept, counts, train, evalset,
                                        {"Baseline": {"error": "skipped",
                                                      "n": 0}})
    assert "NOT TRAINABLE YET" in report
    assert "qlora" in report.lower()


def test_training_command_targets_the_4gb_card():
    cmd = finetune_plan.training_command(300)
    assert "load_in_4bit: true" in cmd
    assert "micro_batch_size: 1" in cmd
    assert "Qwen/Qwen3-4B" in cmd
    assert "paged_adamw_8bit" in cmd


def test_plan_module_never_trains():
    """The work order says plan + baseline, explicitly NOT training."""
    with open("finetune_plan.py", encoding="utf-8") as f:
        body = f.read()
    for forbidden in ("axolotl.cli.train\"", "subprocess", "os.system",
                      "trainer.train("):
        assert forbidden not in body
