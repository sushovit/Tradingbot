"""
Goal 16 — intern v3 + batch grader. No network.
"""

import sqlite3

import intern_desk
import prompts


def test_prompt_v3_contents():
    assert prompts.INTERN_PROMPT_VERSION == 3
    p = prompts.build_intern_desk_prompt("NVDA", "c", "m", "n")
    assert "ADX below 20 = no trend; 20-25 = weak trend" in p
    assert "NEVER describe a value above 25" in p
    assert "downgrade it to no_trade" in p
    assert "ticker-specific" in p


def test_grade_batch_idempotent(temp_journal, tmp_path):
    temp_journal.intern_record("2026-07-24", "NVDA", "long_setup", 75)
    temp_journal.intern_record("2026-07-24", "XOM", "no_trade", 43)

    grades = tmp_path / "grades.txt"
    grades.write_text(
        '# backlog\n'
        '2026-07-24 NVDA good "clean invalidation"\n'
        '2026-07-24 XOM bad "called 43 on a dead chart"\n'
        '2026-07-24 ZZZZ good "no such call"\n')
    rc = intern_desk.grade_batch_cmd(str(grades))
    assert rc == 0                       # unmatched is not a parse failure

    rows = {r["ticker"]: r for r in temp_journal.intern_calls("2026-07-24")}
    assert rows["NVDA"]["grade"] == "good"
    assert rows["XOM"]["grade"] == "bad"

    # Re-run with an updated grade: updates, never duplicates.
    grades.write_text('2026-07-24 NVDA bad "changed my mind"\n')
    intern_desk.grade_batch_cmd(str(grades))
    rows = [r for r in temp_journal.intern_calls("2026-07-24")
            if r["ticker"] == "NVDA"]
    assert len(rows) == 1
    assert rows[0]["grade"] == "bad"


def test_grade_batch_bad_line_flagged(tmp_path, temp_journal):
    grades = tmp_path / "grades.txt"
    grades.write_text("2026-07-24 NVDA excellent note\n")   # invalid grade
    assert intern_desk.grade_batch_cmd(str(grades)) == 1


def test_second_pass_adjusts_midband(temp_journal, monkeypatch):
    verdicts = {
        "AAA": {"stance": "no_trade", "setup_name": None, "conviction": 43,
                "invalidation": None, "key_risk": "", "reasoning": "flat tape"},
        "BBB": {"stance": "no_trade", "setup_name": None, "conviction": 45,
                "invalidation": None, "key_risk": "", "reasoning": "weak base"},
        "CCC": {"stance": "long_setup", "setup_name": "momentum_continuation",
                "conviction": 80, "invalidation": 10.0, "key_risk": "",
                "reasoning": "breakout"},
    }
    monkeypatch.setattr(intern_desk, "_call_local_model",
                        lambda sys_p, user_p: {"AAA": 38, "BBB": 52})
    note = intern_desk.apply_second_pass(verdicts, "2026-07-26")
    assert "2 mid-band" in note
    assert verdicts["AAA"]["conviction"] == 38
    assert verdicts["BBB"]["conviction"] == 52
    assert verdicts["CCC"]["conviction"] == 80          # untouched


def test_second_pass_failure_leaves_scores(temp_journal, monkeypatch):
    verdicts = {
        "AAA": {"stance": "no_trade", "setup_name": None, "conviction": 43,
                "invalidation": None, "key_risk": "", "reasoning": "x"},
        "BBB": {"stance": "no_trade", "setup_name": None, "conviction": 44,
                "invalidation": None, "key_risk": "", "reasoning": "y"},
    }

    def boom(sys_p, user_p):
        raise RuntimeError("ollama down")
    monkeypatch.setattr(intern_desk, "_call_local_model", boom)
    note = intern_desk.apply_second_pass(verdicts, "2026-07-26")
    assert note == ""
    assert verdicts["AAA"]["conviction"] == 43
