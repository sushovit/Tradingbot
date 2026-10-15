"""
S26 — goal sheet + self-report. REPORTING ONLY.

The first block is the HARD RULE: goals never reach the gatekeeper,
prompts.py, the filters, sizing or order code. It fails if "goals" or any
goal key appears in those files, or in the gatekeeper prompt text as it is
actually rendered.
"""

import glob
import json
import os
import re
from datetime import date

import pandas as pd
import pytest

import goals

ROOT = os.path.join(os.path.dirname(__file__), "..")
FORBIDDEN = ("goals",) + goals.GOAL_KEYS
PROTECTED = (["prompts.py", "analyst.py", "risk.py", "claude_integration.py",
              "local_analyst.py", "orders.py", "position_mgmt.py",
              "daily_eval.py"]
             + [os.path.relpath(p, ROOT) for p in
                sorted(glob.glob(os.path.join(ROOT, "strategies", "*.py")))])


def _leaks(text):
    return [w for w in FORBIDDEN if re.search(re.escape(w), text, re.I)]


# ================================================== HARD RULE

@pytest.mark.parametrize("path", PROTECTED)
def test_goals_never_appear_in_trading_code(path):
    text = open(os.path.join(ROOT, path), encoding="utf-8").read()
    assert _leaks(text) == [], f"goal term in {path}"
    assert not re.search(r"^\s*(import goals|from goals import)", text, re.M)


def test_goals_never_appear_in_the_rendered_gatekeeper_prompts():
    import prompts
    texts = [prompts.get_system_prompt(role)
             for role in ("gatekeeper", "junior_analyst", "intern_desk")]
    for setup in ("trend_continuation", "momentum_continuation",
                  "mean_reversion_reclaim", "pullback_in_uptrend",
                  "post_earnings_continuation"):
        texts.append(prompts.build_gatekeeper_user_prompt(
            ticker="NVDA", candle_data_str="o,h,l,c\n1,2,0.5,1.5",
            adx_val=31.0, rsi_val=60.0, ema_spread_pct=0.4,
            volume_trend="rising", crossover_count=1,
            dist_to_resistance_pct=2.0, entry_price=100.0, stop_price=95.0,
            target_price=115.0, rr_ratio=3.0, interval_mins=5, fast_ema=9,
            slow_ema=21, news_str="none", setup_name=setup))
    for text in texts:
        assert _leaks(text) == []


def test_goals_never_appear_in_the_review_models_prompt():
    """The memo's sections 1-5 are model-written; section 6 is appended
    afterwards. The model's own prompt carries no goal."""
    import review_bot
    bundle = {"date": "2026-10-16", "clock": "", "positions": [], "trades": []}
    try:
        user = review_bot.build_user_prompt(bundle)
    except Exception:
        user = ""
    assert _leaks(review_bot.review_system_prompt()) == []
    assert _leaks(user) == []


# ================================================== config

def test_config_carries_the_goal_sheet():
    cfg = json.load(open(os.path.join(ROOT, "bot_config.json"), encoding="utf-8"))
    assert cfg["goals"] == {"trades_per_week": 3, "monthly_pnl_usd": 100,
                            "win_rate_pct": 40, "capture_ratio_pct": 35,
                            "on_time_sessions_pct": 100,
                            "max_profit_to_loss_trades": 0}
    assert goals.load_goals(cfg) == cfg["goals"]
    assert goals.load_goals({}) == goals.DEFAULT_GOALS


# ================================================== the numbers

def _journal(temp_journal, trades, decisions=()):
    """trades: (ticker, buy_ts, buy, qty, stop, sell_ts, sell, reason)."""
    for tk, bts, buy, qty, stop, sts, sell, why in trades:
        did = temp_journal.log_decision(tk, "mean_reversion_reclaim",
                                        {"entry": buy, "stop": stop,
                                         "target": buy + 3 * (buy - stop)},
                                        {"approved": True})
        _stamp(temp_journal, "decisions", did, bts)
        bid = temp_journal.log_trade(tk, "BUY", qty, buy,
                                     reason="mean_reversion_reclaim",
                                     decision_id=did)
        _stamp(temp_journal, "trades", bid, bts)
        if sts:
            sid = temp_journal.log_trade(tk, "SELL", qty, sell,
                                         pnl_usd=(sell - buy) * qty,
                                         reason=why, decision_id=did)
            _stamp(temp_journal, "trades", sid, sts)
    for ts, tk, setup, source, approved, conv, reason in decisions:
        did = temp_journal.log_decision(tk, setup, {"bar": ts[:10]},
                                        {"approved": bool(approved),
                                         "rejection_reason": reason})
        import sqlite3
        c = sqlite3.connect(temp_journal.DB_FILE)
        c.execute("UPDATE decisions SET timestamp=?, source=?, approved=?, "
                  "conviction_score=? WHERE id=?",
                  (ts, source, approved, conv, did))
        c.commit(); c.close()


def _stamp(j, table, row_id, ts):
    import sqlite3
    c = sqlite3.connect(j.DB_FILE)
    c.execute(f"UPDATE {table} SET timestamp=? WHERE id=?", (ts, row_id))
    c.commit(); c.close()


def _bars(points):
    """[(ET 'YYYY-MM-DD HH:MM', high)] -> 5-minute frame."""
    idx = pd.DatetimeIndex([pd.Timestamp(t).tz_localize("America/New_York")
                            for t, _ in points]).tz_convert("UTC")
    highs = [h for _, h in points]
    return pd.DataFrame({"open": highs, "high": highs, "low": highs,
                         "close": highs, "volume": 1}, index=idx)


ASOF = date(2026, 10, 16)                      # a Friday


def _conn(j):
    import sqlite3
    return sqlite3.connect(j.DB_FILE)


def test_week_sheet_from_the_journal(temp_journal):
    _journal(temp_journal, [
        # won: +$30 of a $40 peak -> capture 75%
        ("AMD", "2026-10-12 09:40:00", 100.0, 3, 95.0,
         "2026-10-13 10:00:00", 110.0, "Profit Target"),
        # giveback: up +1R (105) open, closed at 97 (-$6)
        ("ON", "2026-10-13 09:35:00", 100.0, 3, 95.0,
         "2026-10-14 11:00:00", 98.0, "Stop Loss"),
        # still open
        ("MU", "2026-10-15 09:32:00", 50.0, 10, 48.0, None, None, None),
    ], decisions=[
        ("2026-10-12 09:31:00", "X", "trend_continuation", "rules", 0, None, "adx_low"),
        ("2026-10-12 09:32:00", "Y", "trend_continuation", "rules", 0, None, "adx_low"),
        ("2026-10-13 09:33:00", "Z", "mean_reversion_reclaim", "claude", 0, 20,
         "RSI at 81 is overextended"),
        ("2026-10-15 09:34:00", "W", "mean_reversion_reclaim", "claude", 1, 74, None),
        ("2026-10-15 09:35:00", "W", "mean_reversion_reclaim", "rules", 0, None,
         "probation_position_open"),
    ])
    bars = {"AMD": _bars([("2026-10-12 10:00", 113.33), ("2026-10-13 09:55", 108.0)]),
            "ON": _bars([("2026-10-13 10:00", 105.0), ("2026-10-14 10:30", 99.0)])}
    starts = {"2026-10-12": "09:25:05", "2026-10-13": "09:25:07",
              "2026-10-14": "12:06:42", "2026-10-15": "09:25:01"}
    ev = goals.evaluate(_conn(temp_journal), *goals.week_bounds(ASOF), ASOF,
                        goals.DEFAULT_GOALS, None, bars, starts)
    a = ev["actual"]
    assert len(ev["sessions"]) == 5 and ev["fraction"] == 1.0
    assert a["trades_per_week"] == 3.0                 # 3 opened, 5 sessions
    assert a["win_rate_pct"] == 50.0                   # AMD won, ON lost
    assert a["max_profit_to_loss_trades"] == 1         # ON
    # capture: (30 + -6) / (40 + 15) = 43.6%
    assert a["capture_ratio_pct"] == pytest.approx(43.6, abs=0.1)
    # 10-14 started 12:06 (late); 10-16 has neither a log line nor decisions
    assert ev["late"] == ["2026-10-14"] and ev["missed"] == ["2026-10-16"]
    assert a["on_time_sessions_pct"] == 60.0
    f = ev["funnel"]
    assert f["rule_rejections"]["adx_low"] == 2
    assert f["approvals"] == 1 and f["gatekeeper_blocks"]["gatekeeper_rsi"] == 1
    assert f["skipped_after_approval"]["probation slot"] == 1


def test_journal_activity_counts_as_on_time(temp_journal):
    """The laptop-era mornings are not in this Mac's log; a worker that was
    journaling decisions by 09:35 was up at the open."""
    _journal(temp_journal, [], decisions=[
        ("2026-10-12 09:30:43", "X", "trend_continuation", "rules", 0, None, "adx_low"),
        ("2026-10-13 10:30:37", "X", "trend_continuation", "rules", 0, None, "adx_low"),
    ])
    ev = goals.evaluate(_conn(temp_journal), date(2026, 10, 12), date(2026, 10, 13),
                        date(2026, 10, 13), goals.DEFAULT_GOALS, starts={})
    assert ev["late"] == ["2026-10-13"] and ev["missed"] == []
    assert ev["actual"]["on_time_sessions_pct"] == 50.0


def test_periods_never_start_before_the_5k_account(temp_journal):
    ev = goals.evaluate(_conn(temp_journal), *goals.month_bounds(date(2026, 10, 14)),
                        date(2026, 10, 14), goals.DEFAULT_GOALS, starts={})
    assert ev["start"] == "2026-10-09"
    assert ev["sessions"][0] == "2026-10-09" and ev["sessions_in_period"] == 16


def test_month_pnl_is_judged_pro_rata_while_running():
    assert goals.judge("monthly_pnl_usd", 100, 30.0, 0.25, True) == ("MET", 25.0)
    assert goals.judge("monthly_pnl_usd", 100, 20.0, 0.25, True) == ("MISSED", 25.0)
    assert goals.judge("win_rate_pct", 40, None, 1, False)[0] == "NO DATA"
    assert goals.judge("max_profit_to_loss_trades", 0, 0, 1, False)[0] == "MET"
    assert goals.judge("max_profit_to_loss_trades", 0, 1, 1, False)[0] == "MISSED"


def test_sheet_says_why_and_proposes_one_fix_per_miss(temp_journal):
    _journal(temp_journal, [], decisions=[
        ("2026-10-14 09:34:00", "W", "mean_reversion_reclaim", "claude", 1, 74, None),
        ("2026-10-14 09:35:00", "W", "mean_reversion_reclaim", "rules", 0, None,
         "probation_position_open")])
    lines, wk, mo = goals.build(ASOF, _conn(temp_journal), None,
                                goals.DEFAULT_GOALS, bars_by_ticker={},
                                starts={})
    text = "\n".join(lines)
    assert lines[0] == "## 6. Goals"
    assert "| Trades / week | 3 | 0 | **MISSED** |" in text
    fixes = [l for l in lines if "Fix" in l and l.lstrip().startswith("- Fix")]
    misses = text.count("— MISSED.** Why:")
    assert len(fixes) == misses and misses >= 1
    assert all("proposal, CEO to decide" in l or goals.INSUFFICIENT in l
               for l in fixes)
    # the probation skip drives an EXPOSURE proposal; this journal has no
    # tracker replays, so the guard proposes nothing
    probation = next(l for l in fixes if "probation slot" in l)
    assert goals.INSUFFICIENT in probation and "max_concurrent" not in probation
    assert "Profit->loss trades — MET" in text and "Reporting only" in text


def test_weekly_file_and_discord_table(tmp_path, temp_journal):
    lines, wk, mo = goals.build(ASOF, _conn(temp_journal), None,
                                goals.DEFAULT_GOALS, bars_by_ticker={}, starts={})
    path = goals.weekly_file(ASOF, lines, out_dir=str(tmp_path))
    assert os.path.basename(path) == "goals_week_2026-42.md"
    body = open(path, encoding="utf-8").read()
    assert body.startswith("# Goals — week 2026-42") and "Week to date" in body
    msg = goals.discord_table(wk, mo, goals.DEFAULT_GOALS, ASOF)
    assert len(msg) < 2000 and msg.count("```") == 2
    for label in goals.LABELS.values():
        assert label[:20] in msg
    assert "$+" not in msg.split("```")[1].splitlines()[2][:29]   # targets unsigned


# ================================================== review_bot wiring

def test_review_appends_section_6_but_never_journals_it(monkeypatch, tmp_path,
                                                        temp_journal):
    import review_bot
    monkeypatch.chdir(tmp_path)
    posted = []
    monkeypatch.setattr(review_bot, "collect_bundle",
                        lambda date=None: {"date": "2026-10-16", "clock": "c",
                                           "positions": [], "trades": []})
    monkeypatch.setattr(review_bot, "request_review",
                        lambda b: {"text": "## 1. a\n## 5. e\nmemo", "model": "m"})
    monkeypatch.setattr(review_bot, "goals_section",
                        lambda d: "## 6. Goals\n\n| Trades / week | 3 | 0 | **MISSED** |")
    monkeypatch.setattr(review_bot, "post_discord",
                        lambda content, **k: posted.append(content))
    assert review_bot.main([]) == 0
    memo = open(tmp_path / "reports" / "review_2026-10-16.md", encoding="utf-8").read()
    assert memo.index("## 5.") < memo.index("## 6. Goals")
    assert "## 6. Goals" in posted[-1]
    import sqlite3
    c = sqlite3.connect(temp_journal.DB_FILE)
    rows = c.execute("SELECT verdict FROM decisions WHERE source='review_bot'").fetchall()
    c.close()
    assert rows and all("6. Goals" not in r[0] and "trades_per_week" not in r[0]
                        for r in rows)


def test_friday_writes_the_weekly_file_and_posts_the_table(monkeypatch, tmp_path,
                                                           temp_journal):
    import review_bot
    monkeypatch.chdir(tmp_path)
    posted = []
    monkeypatch.setattr(review_bot, "post_discord",
                        lambda content, **k: posted.append(content))
    import broker
    monkeypatch.setattr(broker, "Broker",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    md = review_bot.goals_section("2026-10-16")          # Friday
    assert md.startswith("## 6. Goals")
    assert (tmp_path / "reports" / "goals_week_2026-42.md").exists()
    assert any(p.startswith("**Goals — week 2026-42**") for p in posted)
    posted.clear()
    review_bot.goals_section("2026-10-15")               # Thursday
    assert posted == []
    assert not (tmp_path / "reports" / "goals_week_2026-42.md").read_text() == ""


def test_goal_sheet_failure_never_costs_the_memo(monkeypatch):
    import review_bot
    monkeypatch.setattr(goals, "build",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    out = review_bot.goals_section("2026-10-15")
    assert out.startswith("## 6. Goals") and "unavailable: boom" in out


def test_probation_proposal_follows_the_live_config():
    assert goals._probation_slots({"setup_probation": {"max_concurrent": 1}}) == 1
    assert goals._probation_slots({"setup_probation": {"max_concurrent": 2}}) == 2
    assert goals._probation_slots({}) == 1


# ================================================== exposure guard (CEO, S26)

def _tracker(j, reason_key, rs):
    import sqlite3
    import outcomes
    outcomes.init_table(j.DB_FILE)
    c = sqlite3.connect(j.DB_FILE)
    for i, r in enumerate(rs, 1):
        c.execute("INSERT INTO rejection_outcomes (decision_id, decided_date, "
                  "ticker, source, reason_key, outcome, r_achieved, resolved, "
                  "updated_at) VALUES (?, '2026-10-01', 'X', 'rules', ?, ?, ?, 1, 'x')",
                  (1000 + i, reason_key,
                   "target" if r >= 3 else "stop" if r <= -1 else "neither", r))
    c.commit()
    return c


PROBATION = goals.exposure("raise setup_probation.max_concurrent 2 -> 3",
                           ("probation_position_open",), "the probation slot")


def test_exposure_proposal_without_10_replays_proposes_nothing(temp_journal):
    conn = _tracker(temp_journal, "probation_position_open", [3.0] * 9)
    line = goals.guard(PROBATION, conn)
    assert line.startswith("Fix: insufficient evidence, no change proposed")
    assert "n=9" in line and "max_concurrent" not in line
    assert "proposal, CEO to decide" not in line


def test_exposure_proposal_with_10_replays_cites_n_win_and_avg_r(temp_journal):
    rs = [3.0, 3.0, 3.0, -1.0, -1.0, -1.0, -1.0, 0.5, -0.2, 3.0]
    conn = _tracker(temp_journal, "probation_position_open", rs)
    line = goals.guard(PROBATION, conn)
    assert line.startswith("Fix (proposal, CEO to decide): raise "
                           "setup_probation.max_concurrent 2 -> 3")
    assert "n=10 replayed" in line and "win 50%" in line
    assert f"avg R {sum(rs) / 10:+.2f}" in line


def test_unresolved_or_ungraded_rows_are_not_evidence(temp_journal):
    conn = _tracker(temp_journal, "probation_position_open", [3.0] * 9)
    conn.execute("INSERT INTO rejection_outcomes (decision_id, decided_date, "
                 "ticker, source, reason_key, outcome, r_achieved, resolved, "
                 "updated_at) VALUES (5000, '2026-10-01', 'X', 'rules', "
                 "'probation_position_open', 'no_geometry', NULL, 1, 'x')")
    conn.execute("INSERT INTO rejection_outcomes (decision_id, decided_date, "
                 "ticker, source, reason_key, outcome, r_achieved, resolved, "
                 "updated_at) VALUES (5001, '2026-10-13', 'X', 'rules', "
                 "'probation_position_open', 'neither', 0.4, 0, 'x')")
    assert goals.tracker_evidence(conn, ("probation_position_open",))["n"] == 9


def test_the_conviction_threshold_idea_has_no_tracker_evidence(temp_journal):
    """Approvals below 70 are approved=1 rows; the tracker grades only
    rejections, so this can never clear the guard as things stand."""
    import sqlite3
    conn = sqlite3.connect(temp_journal.DB_FILE)
    fix = goals.exposure("study lowering the gatekeeper's conviction threshold 70",
                         ("conviction < 70",), "the conviction threshold")
    assert goals.INSUFFICIENT in goals.guard(fix, conn)


@pytest.mark.parametrize("text", [
    "raise max_positions 4 -> 5",
    "raise risk_per_trade_pct 1.0 -> 1.25",
    "allow larger position size on conviction 80+",
    "widen the entry window to 09:30-15:30",
    "lower the adx_threshold to 25",
    "loosen the volume filter",
])
def test_unflagged_exposure_wording_is_still_guarded(temp_journal, text):
    import sqlite3
    line = goals.guard(text, sqlite3.connect(temp_journal.DB_FILE))
    assert goals.INSUFFICIENT in line and text not in line


@pytest.mark.parametrize("text", [
    "fix uptime first (missed/late sessions cost whole days)",
    "schedule a 09:15 ET wake (pmset repeat wakeorpoweron) and keep the lid open",
    "study moving the breakeven lock from +1.0R to +0.5R (S15 variants)",
    "study entry quality on stopped trades (stop distance vs ATR) before "
    "changing any filter",
    "commission the journal-only oversold_bounce shadow lane (agenda 14) to "
    "add chop-session supply",
])
def test_non_exposure_fixes_pass_through(temp_journal, text):
    import sqlite3
    line = goals.guard(text, sqlite3.connect(temp_journal.DB_FILE))
    assert line == f"Fix (proposal, CEO to decide): {text}"


def test_every_fix_why_and_fix_can_emit_is_classified_correctly():
    """The two exposure proposals are flagged with evidence keys; every
    plain-string fix in why_and_fix is non-exposure by the keyword net."""
    import inspect
    src = inspect.getsource(goals.why_and_fix)
    assert src.count("exposure(") == 2
    assert '("probation_position_open",)' in src and '("conviction < 70",)' in src
    plain = re.findall(r'fix = \(?"([^"]+)', src)
    assert plain and not any(goals.EXPOSURE_PATTERN.search(t) for t in plain)
