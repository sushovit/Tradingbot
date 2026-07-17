"""
Goal 12 — intern desk. No network, no broker.

  - HARD BOUNDARY: intern_desk imports nothing from broker.py / orders.py
  - verdict normalization (stance, conviction, invalidation, reasoning cap)
  - intern_grades: record, CEO grade, re-run preserves grades
  - report markdown structure
"""

import os
import sqlite3

import intern_desk


def test_hard_boundary_no_broker_or_orders_imports():
    import ast
    src_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "intern_desk.py")
    with open(src_path, encoding="utf-8") as f:
        source = f.read()
    # Real import statements (module-level AND function-level), via AST.
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "").split(".")[0]]
        else:
            continue
        for name in names:
            assert name not in ("broker", "orders"), \
                f"boundary violation: imports {name} (line {node.lineno})"
    # And no trading client anywhere, even hand-constructed.
    assert "TradingClient" not in source


def test_normalize_valid_verdict():
    v = intern_desk.normalize_verdict({
        "stance": "LONG_SETUP", "setup_name": "momentum_continuation",
        "conviction": 74, "invalidation": 58.4, "key_risk": "earnings",
        "reasoning": "One. Two. Three. Four. Five."})
    assert v["stance"] == "long_setup"
    assert v["conviction"] == 74
    assert v["invalidation"] == 58.4
    assert v["reasoning"].count(".") <= 3          # capped at 3 sentences


def test_normalize_rejects_garbage():
    assert intern_desk.normalize_verdict(None) is None
    assert intern_desk.normalize_verdict({"stance": "yolo_long",
                                          "setup_name": None, "conviction": 50,
                                          "invalidation": 1, "key_risk": "",
                                          "reasoning": ""}) is None
    assert intern_desk.normalize_verdict({"stance": "long_setup"}) is None  # missing keys
    # non-numeric conviction -> unusable
    assert intern_desk.normalize_verdict({
        "stance": "long_setup", "setup_name": None, "conviction": "high",
        "invalidation": 1.0, "key_risk": "", "reasoning": ""}) is None


def test_missing_invalidation_kept_as_none():
    v = intern_desk.normalize_verdict({
        "stance": "long_setup", "setup_name": None, "conviction": 80,
        "invalidation": None, "key_risk": "r", "reasoning": "s"})
    assert v is not None
    assert v["invalidation"] is None   # gradeable failure, not a crash


def test_intern_record_and_grade(temp_journal):
    temp_journal.intern_record("2026-07-16", "NVDA", "long_setup", 82)
    assert temp_journal.intern_grade("2026-07-16", "nvda", "good",
                                     "clean invalidation logic")
    # Re-running the desk updates conviction but PRESERVES the grade.
    temp_journal.intern_record("2026-07-16", "NVDA", "long_setup", 79)
    rows = temp_journal.intern_calls("2026-07-16")
    assert len(rows) == 1
    assert rows[0]["conviction"] == 79
    assert rows[0]["grade"] == "good"
    assert rows[0]["grade_note"] == "clean invalidation logic"


def test_grade_unknown_row_returns_false(temp_journal):
    assert temp_journal.intern_grade("2026-07-16", "ZZZZ", "bad", "x") is False


def test_report_markdown_structure():
    verdicts = {
        "NVDA": {"stance": "long_setup", "setup_name": "momentum_continuation",
                 "conviction": 81, "invalidation": 180.5,
                 "key_risk": "chip tariffs", "reasoning": "Strong tape. Volume up."},
        "T": {"stance": "no_trade", "setup_name": None, "conviction": 20,
              "invalidation": None, "key_risk": "none", "reasoning": "Dead money."},
    }
    md = intern_desk.build_markdown("2026-07-16", verdicts, {"XOM": "model call failed"})
    assert "# Intern desk — 2026-07-16" in md
    assert "| NVDA | long_setup | momentum_continuation | 81 | $180.50" in md
    assert "## Top ideas" in md
    assert "### NVDA" in md
    assert "no_trade" not in md.split("## Top ideas")[1]   # no_trade never a top idea
    assert "XOM (model call failed)" in md
