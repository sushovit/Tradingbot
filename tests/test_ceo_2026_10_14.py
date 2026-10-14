"""
CEO orders of 2026-10-14:
  B  dashboard-only code never runs inside the trading worker
  C  the outcome tracker catches up every missed day (journal meta)
  D  the intern desk is off and makes no intern API call anywhere
  E  key copies can never be committed
"""

import datetime as dt
import json
import os
import subprocess

import pytest

import desk_config
import outcomes

ROOT = os.path.join(os.path.dirname(__file__), "..")


# ================================================== B: worker isolation

def test_worker_marks_itself_before_importing_the_page():
    src = open(os.path.join(ROOT, "run_worker.py"), encoding="utf-8").read()
    mark = src.index("os.environ[desk_config.WORKER_ENV] = \"1\"")
    assert mark < src.index("import streamlit_app  # noqa")


def test_dashboard_sections_are_skipped_inside_the_worker():
    src = open(os.path.join(ROOT, "streamlit_app.py"), encoding="utf-8").read()
    charts = src.index("_render_live_charts(broker, broker_err)\n",
                       src.index("with tab1:"))
    assert "if not desk_config.in_worker():" in src[charts - 200:charts]
    table = src.index("trade_table.account_trades(conn")
    guard = src.rindex("if not desk_config.in_worker():", 0, table)
    assert table - guard < 1200


def test_in_worker_reads_the_marker(monkeypatch):
    monkeypatch.delenv(desk_config.WORKER_ENV, raising=False)
    assert desk_config.in_worker() is False
    monkeypatch.setenv(desk_config.WORKER_ENV, "1")
    assert desk_config.in_worker() is True


def test_the_order_path_has_no_alert_wait():
    """CEO audit B, as amended by S19b: alerts on the order path are
    async (post_async) - none may wait or join."""
    src = open(os.path.join(ROOT, "orders.py"), encoding="utf-8").read()
    assert "wait=" not in src


# ================================================== C: catch-up grading

D = dt.date


def test_catch_up_covers_weekends_and_missed_nights():
    # graded through Fri 10-09; the job next runs Wed 10-14 (Mon+Tue missed)
    assert outcomes.catch_up_dates(D(2026, 10, 13), "2026-10-09") == [
        "2026-10-10", "2026-10-11", "2026-10-12", "2026-10-13"]
    assert outcomes.catch_up_dates(D(2026, 10, 13), "2026-10-13") == []
    assert outcomes.catch_up_dates(D(2026, 10, 13), None, "2026-10-12") == [
        "2026-10-13"]
    assert outcomes.catch_up_dates(D(2026, 10, 13), None, None) == ["2026-10-13"]
    assert len(outcomes.catch_up_dates(D(2026, 10, 13), "2026-01-01")) == \
        outcomes.MAX_CATCH_UP_DAYS


def _stub(calls, fail_on=None):
    def evaluate(date_str, db_file=None, include_unresolved=True):
        if date_str and date_str == fail_on:
            raise RuntimeError("network died")
        calls.append((date_str, include_unresolved))
        return {"evaluated": 1, "resolved": 1, "by_outcome": {"stop": 1}}
    return evaluate


def test_nightly_grades_every_missed_day_then_advances_meta(temp_journal):
    temp_journal.set_meta(outcomes.LAST_GRADED_KEY, "2026-10-08")
    calls = []
    total = outcomes.nightly(today=D(2026, 10, 14), evaluate_fn=_stub(calls))
    assert [c[0] for c in calls] == ["2026-10-09", "2026-10-10",
                                     "2026-10-11", "2026-10-12", "2026-10-13"]
    # unresolved rows re-checked once, on the last pass only
    assert [c[1] for c in calls] == [False, False, False, False, True]
    assert temp_journal.get_meta(outcomes.LAST_GRADED_KEY) == "2026-10-13"
    assert total["evaluated"] == 5 and total["by_outcome"] == {"stop": 5}

    calls.clear()                           # same day again: nothing new
    outcomes.nightly(today=D(2026, 10, 14), evaluate_fn=_stub(calls))
    assert calls == [(None, True)]
    assert temp_journal.get_meta(outcomes.LAST_GRADED_KEY) == "2026-10-13"


def test_a_run_killed_half_way_resumes_without_skipping(temp_journal):
    temp_journal.set_meta(outcomes.LAST_GRADED_KEY, "2026-10-08")
    calls = []
    with pytest.raises(RuntimeError):
        outcomes.nightly(today=D(2026, 10, 14),
                         evaluate_fn=_stub(calls, fail_on="2026-10-11"))
    assert temp_journal.get_meta(outcomes.LAST_GRADED_KEY) == "2026-10-10"
    calls.clear()
    outcomes.nightly(today=D(2026, 10, 14), evaluate_fn=_stub(calls))
    assert [c[0] for c in calls] == ["2026-10-11", "2026-10-12", "2026-10-13"]


def test_first_run_seeds_from_the_newest_graded_date(temp_journal):
    outcomes.init_table()
    import sqlite3
    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.execute("INSERT INTO rejection_outcomes (decision_id, decided_date, "
                 "ticker, source, reason_key, outcome, updated_at) VALUES "
                 "(1, '2026-10-12', 'X', 'rules', 'adx_low', 'no_geometry', 'x')")
    conn.commit(); conn.close()
    calls = []
    outcomes.nightly(today=D(2026, 10, 14), evaluate_fn=_stub(calls))
    assert [c[0] for c in calls] == ["2026-10-13"]


def test_nightly_really_grades_a_missed_friday(temp_journal):
    """End to end with the real evaluate(): a Friday rejection, graded on
    the following Tuesday after Monday's run never happened."""
    import pandas as pd
    did = temp_journal.log_decision("AAA", "mean_reversion_reclaim",
                                    {"entry": 100.0, "stop": 95.0,
                                     "target": 115.0}, {"approved": False})
    import sqlite3
    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.execute("UPDATE decisions SET timestamp='2026-10-09 09:40:00', "
                 "source='claude', approved=0 WHERE id=?", (did,))
    conn.commit(); conn.close()
    temp_journal.set_meta(outcomes.LAST_GRADED_KEY, "2026-10-08")
    idx = pd.to_datetime(["2026-10-09", "2026-10-12", "2026-10-13"]).tz_localize("UTC")
    bars = pd.DataFrame({"open": [100, 101, 102], "high": [101, 116, 103],
                         "low": [99, 100, 101], "close": [100, 115, 102],
                         "volume": [1, 1, 1]}, index=idx)

    def evaluate(date_str, db_file=None, include_unresolved=True):
        return outcomes.evaluate(date_str, bars_by_ticker={"AAA": bars},
                                 db_file=db_file,
                                 include_unresolved=include_unresolved)
    outcomes.nightly(today=D(2026, 10, 14), evaluate_fn=evaluate)
    conn = sqlite3.connect(temp_journal.DB_FILE)
    row = conn.execute("SELECT decided_date, outcome, r_achieved FROM "
                       "rejection_outcomes WHERE decision_id=?", (did,)).fetchone()
    conn.close()
    assert row == ("2026-10-09", "target", 3.0)


def test_cli_default_path_is_the_catch_up(monkeypatch, capsys):
    monkeypatch.setattr(outcomes, "nightly", lambda: {
        "evaluated": 2, "resolved": 2, "by_outcome": {"stop": 2},
        "dates": ["2026-10-12", "2026-10-13"]})
    monkeypatch.setattr("sys.argv", ["outcomes.py"])
    assert outcomes.main() == 0
    assert "Graded 2026-10-12..2026-10-13" in capsys.readouterr().out


# ================================================== D: intern desk off

def test_shipped_config_has_the_intern_desk_off():
    cfg = json.load(open(os.path.join(ROOT, "bot_config.json"), encoding="utf-8"))
    assert cfg["intern"] == {"enabled": False}
    assert desk_config.intern_enabled(cfg) is False
    assert desk_config.intern_enabled({}) is False        # off by omission
    assert desk_config.intern_enabled({"intern": {"enabled": True}}) is True


def test_intern_desk_refuses_to_scan_or_trade_while_off(monkeypatch, capsys):
    import intern_desk
    monkeypatch.setattr(desk_config, "intern_enabled", lambda config=None: False)
    monkeypatch.setattr(intern_desk, "run_desk",
                        lambda *a, **k: pytest.fail("intern desk ran"))
    for argv in (["intern_desk.py"], ["intern_desk.py", "--trade"]):
        monkeypatch.setattr("sys.argv", argv)
        assert intern_desk.main() == 0
    assert "disabled" in capsys.readouterr().out


def test_report_checks_the_switch_before_building_an_intern_broker():
    src = open(os.path.join(ROOT, "report.py"), encoding="utf-8").read()
    section = src[src.index("## Intern account"):]
    assert section.index("raise _InternOff()") < section.index('_B(account="intern")')
    assert "except _InternOff:" in section


def test_install_skips_the_intern_job_by_default():
    sh = open(os.path.join(ROOT, "jobs", "macos", "install.sh"),
              encoding="utf-8").read()
    assert '"$label" = "com.tradingbot.intern"' in sh
    assert '${INSTALL_INTERN:-0}' in sh and "continue" in sh
    assert os.path.exists(os.path.join(ROOT, "jobs", "macos",
                                       "com.tradingbot.intern.plist"))


# ================================================== E: key copies

@pytest.mark.parametrize("path, ignored", [
    (".env", True), (".env.laptop", True), (".env.bak", True),
    (".env.cutover-2026-10-09", True), ("keys.cutover-old", True),
    ("alpaca.cutover-backup.txt", True), (".env.example", False),
])
def test_key_copies_are_ignored(path, ignored):
    r = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
    assert (r.returncode == 0) is ignored
