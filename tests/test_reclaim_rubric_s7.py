"""
S7 (agenda 10.2): setup-specific gatekeeper rules for mean_reversion_reclaim,
prompt_version v5, and reclaim back on probation counted from v5. No network.

THE EVIDENCE. Week of 2026-09-08: 27 reclaims reached the gatekeeper and 3
were approved. Nineteen of the 24 rejections cited low ADX or sub-45 RSI. A
washout-and-reclaim IS a low-ADX, depressed-RSI pattern — those are its
entry conditions, not its flaws. The prompt was grading it with
trend-continuation rules, so the gate was rejecting the setup for being
itself, against a 3-year backtest of +0.37R over 785 trades.

WHY THE VERSION MATTERS. A prompt change splits the statistics. A reclaim
approval rate measured under v4 and one measured under v5 describe two
different gates, and pooling them would hide exactly the effect this order
exists to measure. So every verdict carries its version, and reclaim's
probation counts from v5 only — the setup has live history, but under a gate
nobody has tested.
"""

import json

import pytest

import analyst
import journal as journal_mod
import prompts
import risk


CONFIG = json.load(open("bot_config.json", encoding="utf-8"))

# The ADX language the reclaim rubric exists to remove.
ADX_REJECT = "ADX < 20 (market is ranging"
ADX_APPROVE = "ADX > 25 (trending market, not ranging)"

METRICS = dict(ticker="SWKS", candle_data_str="(candles)", adx_val=17.2,
               rsi_val=41.0, ema_spread_pct=0.4, volume_trend="increasing",
               crossover_count=1, dist_to_resistance_pct=1.2,
               entry_price=89.86, stop_price=74.94, target_price=127.54,
               rr_ratio=2.5, interval_mins=5, fast_ema=9, slow_ema=21,
               news_str="No recent news available.")


def prompt_for(setup_name):
    return prompts.build_gatekeeper_user_prompt(setup_name=setup_name,
                                                **METRICS)


RECLAIM = prompt_for("mean_reversion_reclaim")
MOMENTUM = prompt_for("momentum_continuation")


# ============================================ the reclaim rubric

def test_the_reclaim_prompt_carries_its_own_decision_rules():
    assert "=== DECISION RULES - mean_reversion_reclaim ===" in RECLAIM


def test_adx_is_informational_not_a_rejection_criterion():
    assert "ADX IS INFORMATIONAL HERE" in RECLAIM
    assert "NOT a rejection criterion at any value" in RECLAIM
    assert "do not cite a" in RECLAIM and "ranging market" in RECLAIM


def test_the_adx_reject_language_is_gone_from_the_reclaim_prompt():
    """The headline requirement. Nineteen of 24 rejections came from these
    two lines."""
    assert ADX_REJECT not in RECLAIM
    assert ADX_APPROVE not in RECLAIM


def test_the_adx_metric_annotation_no_longer_contradicts_the_rubric():
    """A rubric saying ADX is informational, above a metrics line saying
    '<20 = ranging/avoid', is the same contradiction in a smaller font."""
    assert "ranging/avoid" not in RECLAIM
    assert "- ADX: 17.2 (informational for this setup" in RECLAIM


def test_rsi_is_a_direction_test_not_a_floor():
    assert "judge DIRECTION, not level" in RECLAIM
    assert "There is no RSI floor for this setup" in RECLAIM
    assert "RSI is RISING over the last 3 bars" in RECLAIM
    assert "<45 = weak momentum" not in RECLAIM       # the annotation too


def test_the_four_approval_conditions_are_all_present():
    for clause in ("RSI is RISING over the last 3 bars",
                   "Reclaim-bar volume >= the 20-bar average volume",
                   "The close is ABOVE the reclaim level",
                   "No earnings announcement within 5 sessions"):
        assert clause in RECLAIM, clause


def test_the_two_named_rejections_survive():
    """The order keeps exactly two hard rejections from the old rules."""
    assert "RSI > 75 (overextended at entry" in RECLAIM
    assert "An earnings announcement within 5 sessions" in RECLAIM


def test_everything_else_is_a_conviction_input():
    """Without this line the model treats the approve-list as exhaustive and
    finds a new reason to decline."""
    assert "Everything else is a conviction input, not a veto." in RECLAIM


def test_the_near_resistance_guidance_still_applies():
    """S7 must not drop the ratified calibration that came before it."""
    assert "proximity to the 20-bar high" in RECLAIM
    assert "+0.208R" in RECLAIM


# ============================================ nothing else moved

@pytest.mark.parametrize("setup", ["trend_continuation", "momentum_continuation",
                                   "pullback_in_uptrend",
                                   "post_earnings_continuation"])
def test_every_other_setup_keeps_the_rules_it_had(setup):
    rendered = prompt_for(setup)
    assert prompts.DEFAULT_DECISION_RULES.strip() in rendered
    assert ADX_REJECT in rendered
    assert ADX_APPROVE in rendered
    assert "ranging/avoid" in rendered
    assert "<45 = weak momentum" in rendered


def test_an_unknown_setup_falls_back_to_the_default_rules():
    """Fail-closed: a setup added later is graded by the strict rules until
    someone writes it a rubric, not by the loosest block available."""
    assert prompts.decision_rules_for("something_new") is \
        prompts.DEFAULT_DECISION_RULES


def test_the_momentum_guidance_block_is_untouched():
    assert "the entry IS a break of the\n20-bar high" in MOMENTUM
    assert "Rejecting a breakout because it is near the level it just broke" \
        in MOMENTUM


# ============================================ prompt_version

def test_the_version_is_five():
    assert prompts.GATEKEEPER_PROMPT_VERSION == 5


def make_analyst(monkeypatch, temp_journal, verdict=None):
    verdict = verdict or {"approved": True, "conviction_score": 80,
                          "reasoning": "clean reclaim"}
    monkeypatch.setattr(analyst, "journal", temp_journal)
    monkeypatch.setattr(analyst.claude_integration, "get_gatekeeper_decision",
                        lambda **k: dict(verdict))
    monkeypatch.setattr(analyst.local_analyst, "get_gatekeeper_decision",
                        lambda **k: dict(verdict))
    return verdict


def journaled_context(temp_journal, decision_id):
    import sqlite3
    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT context FROM decisions WHERE id=?",
                       (decision_id,)).fetchone()
    conn.close()
    return json.loads(row["context"])


def test_a_claude_verdict_is_journaled_with_the_version(monkeypatch,
                                                        temp_journal):
    make_analyst(monkeypatch, temp_journal)
    _, decision_id = analyst.get_verdict("claude", "SWKS",
                                         "mean_reversion_reclaim",
                                         {"entry": 89.86}, {})
    assert journaled_context(temp_journal, decision_id)["prompt_version"] == 5


def test_a_local_verdict_is_journaled_with_the_version(monkeypatch,
                                                       temp_journal):
    """Every verdict — the order says so, and a local-mode row without it
    would be an unversioned hole in the same table."""
    make_analyst(monkeypatch, temp_journal)
    _, decision_id = analyst.get_verdict("local", "SWKS",
                                         "mean_reversion_reclaim", {}, {})
    assert journaled_context(temp_journal, decision_id)["prompt_version"] == 5


def test_the_shadow_row_inherits_it(monkeypatch, temp_journal):
    verdict = make_analyst(monkeypatch, temp_journal)
    context = {"entry": 89.86, "prompt_version":
               prompts.GATEKEEPER_PROMPT_VERSION}
    analyst._shadow_worker({}, "SWKS", "mean_reversion_reclaim", context,
                           verdict)
    import sqlite3
    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT context FROM decisions WHERE "
                       "source='local_shadow' ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    assert json.loads(row["context"])["prompt_version"] == 5


def test_stamping_does_not_mutate_the_caller_s_context(monkeypatch,
                                                       temp_journal):
    """The worker reuses its decision_context; get_verdict must not write
    into the dict it was handed."""
    make_analyst(monkeypatch, temp_journal)
    caller = {"entry": 89.86}
    analyst.get_verdict("claude", "SWKS", "mean_reversion_reclaim", caller, {})
    assert "prompt_version" not in caller


# ============================================ probation counted from v5

def test_the_config_puts_reclaim_on_probation_from_v5():
    prob = CONFIG["setup_probation"]
    assert "mean_reversion_reclaim" in prob["setups"]
    assert prob["trades"] == 20
    assert prob["max_concurrent"] == 1
    assert prob["count_from_prompt_version"]["mean_reversion_reclaim"] == 5
    assert risk.probation_min_prompt_version("mean_reversion_reclaim",
                                             CONFIG) == 5


def test_the_other_probation_setups_are_not_version_scoped():
    """They went live under v4 and their counts are already meaningful."""
    for name in ("pullback_in_uptrend", "post_earnings_continuation"):
        assert risk.probation_min_prompt_version(name, CONFIG) is None


def buy_under(temp_journal, prompt_version, ticker="SWKS"):
    """One live reclaim entry whose verdict came from `prompt_version`."""
    context = {} if prompt_version is None else {"prompt_version": prompt_version}
    decision_id = temp_journal.log_decision(
        ticker, "mean_reversion_reclaim", context,
        {"approved": True, "conviction_score": 80})
    return temp_journal.log_trade(ticker, "BUY", 1, 100.0,
                                  reason="mean_reversion_reclaim",
                                  decision_id=decision_id)


def test_the_probation_count_ignores_pre_v5_entries(temp_journal):
    """The headline requirement. Four v4 entries and one v5 entry is a
    probation count of ONE."""
    for _ in range(4):
        buy_under(temp_journal, 4)
    buy_under(temp_journal, 5)

    assert temp_journal.live_entry_count("mean_reversion_reclaim") == 5
    assert temp_journal.live_entry_count("mean_reversion_reclaim",
                                         min_prompt_version=5) == 1


def test_an_entry_with_no_linked_decision_is_pre_v5(temp_journal):
    """The live SWKS and SPCX rows predate versioning entirely. Absence of a
    version is not permission to count it."""
    temp_journal.log_trade("SPCX", "BUY", 1, 154.25,
                           reason="mean_reversion_reclaim")
    buy_under(temp_journal, None)
    assert temp_journal.live_entry_count("mean_reversion_reclaim",
                                         min_prompt_version=5) == 0


def test_a_later_prompt_version_still_counts(temp_journal):
    """v6 must not reset the count — the scope is a floor, not an equality."""
    buy_under(temp_journal, 6)
    assert temp_journal.live_entry_count("mean_reversion_reclaim",
                                         min_prompt_version=5) == 1


def test_the_count_is_still_per_setup(temp_journal):
    decision_id = temp_journal.log_decision(
        "ARM", "pullback_in_uptrend", {"prompt_version": 5},
        {"approved": True})
    temp_journal.log_trade("ARM", "BUY", 1, 270.11, reason="pullback_in_uptrend",
                           decision_id=decision_id)
    assert temp_journal.live_entry_count("mean_reversion_reclaim",
                                         min_prompt_version=5) == 0


def test_reclaim_is_on_probation_at_zero_v5_entries(temp_journal):
    """The practical consequence: from Monday, one reclaim at a time."""
    for _ in range(9):
        buy_under(temp_journal, 4)
    live_n = temp_journal.live_entry_count(
        "mean_reversion_reclaim",
        risk.probation_min_prompt_version("mean_reversion_reclaim", CONFIG))
    assert live_n == 0
    assert risk.on_probation("mean_reversion_reclaim", live_n, CONFIG) is True
    ok, reason = risk.check_setup_probation("mean_reversion_reclaim",
                                            open_for_setup=1,
                                            live_trades=live_n, config=CONFIG)
    assert ok is False and reason == "probation_position_open"


def test_it_graduates_at_twenty_v5_entries(temp_journal):
    for _ in range(20):
        buy_under(temp_journal, 5)
    live_n = temp_journal.live_entry_count("mean_reversion_reclaim", 5)
    assert live_n == 20
    assert risk.on_probation("mean_reversion_reclaim", live_n, CONFIG) is False


def test_setup_live_counts_scopes_each_setup_to_its_own_window(temp_journal):
    buy_under(temp_journal, 4)
    decision_id = temp_journal.log_decision(
        "ARM", "pullback_in_uptrend", {"prompt_version": 4}, {"approved": True})
    temp_journal.log_trade("ARM", "BUY", 1, 270.11, reason="pullback_in_uptrend",
                           decision_id=decision_id)

    counts = temp_journal.setup_live_counts(
        ["mean_reversion_reclaim", "pullback_in_uptrend"], CONFIG)
    assert counts["mean_reversion_reclaim"] == 0     # v4, scoped out
    assert counts["pullback_in_uptrend"] == 1        # not version-scoped


def test_an_absent_scope_counts_everything(temp_journal):
    """Back-compatibility: no config, no scoping."""
    buy_under(temp_journal, 4)
    assert temp_journal.setup_live_counts(
        ["mean_reversion_reclaim"])["mean_reversion_reclaim"] == 1
    assert risk.probation_min_prompt_version("mean_reversion_reclaim",
                                             {}) is None


def test_the_worker_counts_with_the_scope():
    with open("streamlit_app.py", encoding="utf-8") as f:
        src = f.read()
    idx = src.index("live_n = journal.live_entry_count(")
    assert "risk.probation_min_prompt_version(" in src[idx:idx + 300]
