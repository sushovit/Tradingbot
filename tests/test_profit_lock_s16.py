"""
S16 — profit lock for daily positions (CEO 2026-10-09, reversing 10.6).

A daily position on `daily_trailing: "profit_lock"` steps its stop up in R
steps decided on the last COMPLETED 5-minute close:
[[trigger_R, stop_R], ...]. Never lowers a stop, never places one at or
above the market, and is a no-op on restart. Intraday is untouched.
"""

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import position_mgmt as pm
import review_bot

PROFILE = {"trailing_stop_type": "ATR", "trailing_stop_value": 2.5,
           "daily_trailing": "profit_lock",
           "daily_profit_lock": [[1.5, 0.5], [2.5, 1.5]]}
NOW = datetime(2026, 10, 9, 15, 0, 30, tzinfo=timezone.utc)   # 11:00:30 ET


class Order:
    def __init__(self, oid):
        self.id = oid


class FakeBroker:
    def __init__(self):
        self.replaced = []

    def replace_stop(self, order_id, price):
        self.replaced.append((order_id, price))
        return Order(f"stop-{len(self.replaced)}")


def spcx(**over):
    """SPCX as positions.json holds it: entry 154.25, structural stop
    144.445, no original_stop yet. R = 9.805."""
    state = {"in_position": True, "source": "bot", "timeframe": "daily",
             "setup": "mean_reversion_reclaim", "entry_price": 154.25,
             "initial_stop": 144.445, "trailing_stop_price": 144.445,
             "stop_order_id": "stop-0", "shares_held": 1,
             "decision_id": 7892}
    state.update(over)
    return state


def bars(*closes, last_forming=None, now=NOW):
    """5-minute bars stamped with their START time (as Alpaca does); the
    last `closes` bar is the newest COMPLETED one. `last_forming` adds a
    still-open bar after it."""
    start = pd.Timestamp(now).floor("5min") - pd.Timedelta(minutes=5)
    rows = list(closes) + ([last_forming] if last_forming is not None else [])
    n_done = len(closes)
    idx = [start - pd.Timedelta(minutes=5 * (n_done - 1 - i))
           for i in range(len(rows))]
    if last_forming is not None:
        idx[-1] = start + pd.Timedelta(minutes=5)
    return pd.DataFrame({"open": rows, "high": rows, "low": rows,
                         "close": rows}, index=pd.DatetimeIndex(idx))


def run(state, df, current_price, profile=PROFILE, now=NOW):
    broker, journal, persisted = FakeBroker(), [], []
    positions = {"SPCX": state}
    order = []

    def persist(p):
        order.append("persist")
        persisted.append(json.loads(json.dumps(p)))

    def journal_raise(t, q, px, why):
        order.append("journal")
        journal.append((t, q, px, why))

    changed = pm.maybe_lock_profit(broker, positions, "SPCX", state, df,
                                   profile, current_price, now,
                                   persist=persist,
                                   journal_raise=journal_raise)
    return changed, broker, journal, persisted, positions, order


R = 154.25 - 144.445
STEP1_TRIGGER = 154.25 + 1.5 * R          # 168.9575
STEP1_STOP = 159.16                       # 159.1525 rounded UP to the cent
STEP2_TRIGGER = 154.25 + 2.5 * R          # 178.7625
STEP2_STOP = 168.96                       # 168.9575 rounded up


# ================================================== config parsing

def test_steps_parse_sorted():
    assert pm.profit_lock_steps(
        {"daily_profit_lock": [[2.5, 1.5], [1.5, 0.5]]}) == ((1.5, 0.5),
                                                               (2.5, 1.5))


@pytest.mark.parametrize("raw", [None, [], "1.5,0.5", [[1.5]], [["x", 0.5]],
                                 [[1.0, 1.0]], [[1.0, 2.0]],
                                 [[float("nan"), 0.5]]])
def test_malformed_or_unplaceable_steps_are_dropped(raw):
    """A stop at/above its own trigger could never sit below the market."""
    assert pm.profit_lock_steps({"daily_profit_lock": raw}) == ()


def test_the_shipped_config_is_breakeven_at_one_r():
    """CEO rule 2026-10-09: f did not beat b on expectancy for both setups,
    so b ships - stop to breakeven at +1R."""
    cfg = json.load(open("bot_config.json", encoding="utf-8"))
    for name in ("Aggressive", "Moderate"):
        profile = cfg["risk_profiles"][name]
        assert profile["daily_trailing"] == "profit_lock"
        assert pm.profit_lock_steps(profile) == ((1.0, 0.0),)


def test_breakeven_step_moves_the_stop_to_entry():
    state = spcx()
    profile = dict(PROFILE, daily_profit_lock=[[1.0, 0.0]])
    changed, broker, _, _, positions, _ = run(state, bars(164.2), 164.3,
                                              profile)
    assert changed and broker.replaced == [("stop-0", 154.25)]
    assert positions["SPCX"]["trailing_stop_price"] == 154.25


def test_r_uses_the_broker_corrected_entry_price():
    """PLTR: positions.json held 201.235, the broker fill was 202.815 and
    sync corrects entry_price to it. R must be 202.815 - 195.75 = 7.065, so
    +1R is 209.88 - not 206.72 off the stale entry."""
    profile = dict(PROFILE, daily_profit_lock=[[1.0, 0.0]])
    state = spcx(entry_price=202.815, initial_stop=195.75,
                 trailing_stop_price=195.75, original_stop=195.75)
    changed, broker, _, _, _, _ = run(state, bars(207.0), 207.0, profile)
    assert changed is False and broker.replaced == []    # +1R off the stale
    changed, broker, _, _, _, _ = run(state, bars(209.9), 210.0, profile)
    # Breakeven never rounds below entry: 202.815 -> 202.82, not 202.81.
    assert changed and broker.replaced == [("stop-0", 202.82)]


def test_trailing_type_resolves_profit_lock_for_daily_only():
    assert pm.trailing_type_for({"timeframe": "daily"}, PROFILE) == "profit_lock"
    assert pm.trailing_type_for({"timeframe": "intraday"}, PROFILE) == "atr"


# ================================================== the steps

def test_no_change_below_the_first_trigger():
    """SPCX at +0.9R: the structural stop stands."""
    state = spcx()
    changed, broker, journal, _, _, _ = run(state, bars(163.0), 163.1)
    assert changed is False and broker.replaced == [] and journal == []
    assert state["trailing_stop_price"] == 144.445


def test_step_one_raises_to_plus_half_r():
    state = spcx()
    changed, broker, journal, persisted, positions, order = run(
        state, bars(165.0, 169.5), 169.6)
    assert changed is True
    assert broker.replaced == [("stop-0", STEP1_STOP)]
    assert positions["SPCX"]["trailing_stop_price"] == STEP1_STOP
    assert positions["SPCX"]["stop_order_id"] == "stop-1"
    assert positions["SPCX"]["profit_lock_step"] == 1
    t, q, px, why = journal[0]
    assert (t, q, px) == ("SPCX", 1, STEP1_STOP)
    assert "step 1" in why and "144.445 -> 159.16" in why
    # Journal the raise, THEN write_positions (the order's sequence).
    assert order[-2:] == ["journal", "persist"]
    assert persisted[-1]["SPCX"]["trailing_stop_price"] == STEP1_STOP


def test_step_two_raises_from_step_one():
    state = spcx(trailing_stop_price=STEP1_STOP, stop_order_id="stop-1",
                 original_stop=144.445, profit_lock_step=1)
    changed, broker, journal, _, positions, _ = run(state, bars(179.0), 179.2)
    assert changed is True
    assert broker.replaced == [("stop-1", STEP2_STOP)]
    assert positions["SPCX"]["profit_lock_step"] == 2
    assert "step 2" in journal[0][3]


def test_a_gap_past_both_triggers_goes_straight_to_step_two():
    state = spcx()
    changed, broker, _, _, positions, _ = run(state, bars(180.0), 180.1)
    assert changed and broker.replaced == [("stop-0", STEP2_STOP)]
    assert positions["SPCX"]["profit_lock_step"] == 2


def test_never_lowers_a_stop():
    """Price falls back below step 2's trigger after step 2: nothing moves,
    and a stop already above the step level is never pulled down."""
    state = spcx(trailing_stop_price=STEP2_STOP, original_stop=144.445)
    changed, broker, _, _, _, _ = run(state, bars(170.0), 170.0)
    assert changed is False and broker.replaced == []
    assert state["trailing_stop_price"] == STEP2_STOP
    higher = spcx(trailing_stop_price=175.0, original_stop=144.445)
    changed, broker, _, _, _, _ = run(higher, bars(180.0), 180.0)
    assert changed is False and broker.replaced == []


def test_a_restart_does_not_replace_twice():
    """The raised level is persisted; the next process computes the same
    step and does nothing."""
    state = spcx()
    _, _, _, persisted, _, _ = run(state, bars(169.5), 169.6)
    reloaded = persisted[-1]["SPCX"]
    changed, broker, journal, _, _, _ = run(reloaded, bars(169.7), 169.8)
    assert changed is False and broker.replaced == [] and journal == []


def test_only_a_completed_bar_counts():
    """The newest bar is still forming: its 170 close is not a close yet."""
    state = spcx()
    df = bars(166.0, last_forming=170.0)
    changed, broker, _, _, _, _ = run(state, df, 170.0)
    assert changed is False and broker.replaced == []
    assert pm.last_completed_close(df, NOW) == 166.0


def test_no_stop_at_or_above_the_market():
    """Completed close crossed the trigger but price has since fallen under
    the step's stop: placing it would fill instantly - skip."""
    state = spcx()
    changed, broker, _, _, _, _ = run(state, bars(169.5), STEP1_STOP - 0.01)
    assert changed is False and broker.replaced == []


def test_original_stop_is_stored_from_the_structural_stop():
    """SPCX has no original_stop on file; it is backfilled from
    initial_stop (144.445) and persisted, even when no step fires."""
    state = spcx()
    _, _, _, persisted, positions, _ = run(state, bars(160.0), 160.0)
    assert positions["SPCX"]["original_stop"] == 144.445
    assert persisted[-1]["SPCX"]["original_stop"] == 144.445


def test_r_is_measured_from_the_original_stop_not_the_raised_one():
    """After step 1 the live stop is 159.15; R must stay 9.805, or step 2
    would trigger far too early."""
    state = spcx(trailing_stop_price=STEP1_STOP, original_stop=144.445)
    changed, broker, _, _, _, _ = run(state, bars(175.0), 175.0)
    assert changed is False and broker.replaced == []    # < 178.76


def test_ceo_and_unknown_positions_are_never_touched():
    for source in ("ceo", "unknown", None):
        state = spcx(source=source)
        changed, broker, _, _, _, _ = run(state, bars(180.0), 180.0)
        assert changed is False and broker.replaced == []


def test_no_steps_configured_means_no_lock():
    state = spcx()
    profile = dict(PROFILE, daily_profit_lock=[])
    changed, broker, _, _, _, _ = run(state, bars(180.0), 180.0, profile)
    assert changed is False and broker.replaced == []


# ================================================== intraday unchanged

def atr_bars(n=60, price=100.0):
    idx = pd.date_range("2026-10-09 13:30", periods=n, freq="5min", tz="UTC")
    return pd.DataFrame({"open": price, "high": price + 1, "low": price - 1,
                         "close": price, "volume": 1000}, index=idx)


def test_profit_lock_ignores_intraday_positions():
    state = spcx(timeframe="intraday")
    changed, broker, _, _, _, _ = run(state, bars(180.0), 180.0)
    assert changed is False and broker.replaced == []


def test_the_atr_ratchet_never_acts_on_a_profit_lock_position():
    """Without the guard, compute_trailing_stop would read the unknown type
    as the PERCENT rule and trail the daily stop."""
    broker = FakeBroker()
    state = spcx(reached_1r=True)
    assert pm.maybe_ratchet_stop(broker, {"SPCX": state}, "SPCX", state,
                                 atr_bars(price=180.0), PROFILE,
                                 180.0) is False
    assert broker.replaced == []


def test_intraday_still_ratchets_with_atr_and_the_floor():
    broker = FakeBroker()
    state = {"in_position": True, "source": "bot", "timeframe": "intraday",
             "entry_price": 100.0, "initial_stop": 95.0,
             "trailing_stop_price": 95.0, "stop_order_id": "stop-0"}
    positions = {"X": state}
    assert pm.maybe_ratchet_stop(broker, positions, "X", state,
                                 atr_bars(price=112.0), PROFILE, 112.0) is True
    new_stop = positions["X"]["trailing_stop_price"]
    assert new_stop >= 100.0           # breakeven floor at/after +1R
    assert broker.replaced and broker.replaced[0][0] == "stop-0"


# ================================================== wiring and prompt

def test_the_worker_routes_daily_profit_lock_through_maybe_lock_profit():
    with open("streamlit_app.py", encoding="utf-8") as f:
        src = f.read()
    idx = src.index("position_mgmt.maybe_lock_profit(")
    block = src[idx - 400:idx + 900]
    assert "== position_mgmt.PROFIT_LOCK" in block
    assert "persist=write_positions" in block
    assert '"TIGHTEN_STOP"' in block
    assert "interval_minutes=interval_mins" in block
    assert "trail_df = None" in block      # no ATR ratchet on this path


def test_the_review_prompt_states_the_new_rule():
    prompt = review_bot.REVIEW_SYSTEM_PROMPT
    assert "PROFIT LOCK" in prompt and "reversing agenda 10.6" in prompt
    assert "daily_profit_lock" in prompt
    assert "max(ATR trail, entry price)" in prompt     # intraday unchanged
    assert "static stop" not in prompt.replace("10.6's static stop", "")


# ================================================== probation exclusion

import floor  # noqa: E402
import risk  # noqa: E402


def reclaim_buy(journal, version=5, ticker="PLTR"):
    decision_id = journal.log_decision(ticker, "mean_reversion_reclaim",
                                       {"prompt_version": version},
                                       {"approved": True})
    return journal.log_trade(ticker, "BUY", 2, 202.815,
                             reason="mean_reversion_reclaim",
                             decision_id=decision_id)


def test_exclude_trade_ids_parse():
    assert risk.probation_exclude_trade_ids(
        {"setup_probation": {"exclude_trade_ids": [37, "38", "x", None]}}) \
        == (37, 38)
    assert risk.probation_exclude_trade_ids({}) == ()


def test_the_shipped_config_excludes_row_37():
    cfg = json.load(open("bot_config.json", encoding="utf-8"))
    assert risk.probation_exclude_trade_ids(cfg) == (37,)


def test_an_excluded_buy_leaves_the_probation_count(temp_journal):
    kept = reclaim_buy(temp_journal, ticker="LRCX")
    dropped = reclaim_buy(temp_journal)
    count = lambda ex: temp_journal.live_entry_count(
        "mean_reversion_reclaim", 5, exclude_trade_ids=ex)
    assert count(()) == 2
    assert count((dropped,)) == 1
    assert count((dropped, kept)) == 0
    # The unscoped count honours it too.
    assert temp_journal.live_entry_count(
        "mean_reversion_reclaim", exclude_trade_ids=(dropped,)) == 1
    cfg = {"setup_probation": {"count_from_prompt_version":
                               {"mean_reversion_reclaim": 5},
                               "exclude_trade_ids": [dropped]}}
    assert temp_journal.setup_live_counts(
        ["mean_reversion_reclaim"], cfg)["mean_reversion_reclaim"] == 1


def test_gate_floor_and_report_all_pass_the_exclusion():
    for path in ("streamlit_app.py", "floor.py", "report.py"):
        with open(path, encoding="utf-8") as f:
            src = f.read()
        idx = src.index("live_entry_count(")
        assert "probation_exclude_trade_ids(" in src[idx:idx + 400], path


def test_the_floor_line_reads_zero_with_row_37_excluded(tmp_path, monkeypatch,
                                                         temp_journal):
    import shutil
    pltr_id = reclaim_buy(temp_journal)
    cfg = json.load(open("bot_config.json", encoding="utf-8"))
    cfg["setup_probation"]["exclude_trade_ids"] = [pltr_id]
    (tmp_path / "bot_config.json").write_text(json.dumps(cfg))
    (tmp_path / "positions.json").write_text("{}")
    monkeypatch.chdir(tmp_path)
    line = next(l for l in floor.probation_section()
                if "mean_reversion_reclaim" in l)
    assert "0/20 probation" in line
