"""
export_training.py — audit + export the journal as a fine-tune candidate set.

    python export_training.py [--out training_candidate.jsonl]

Produces training_candidate.jsonl (one JSON object per line) and prints an
audit: counts by source and prompt_version, % graded, % with linked
outcomes, and every row EXCLUDED with the reason.

NO TRAINING HAPPENS HERE. This answers "what do we actually have, and what
is missing?" before any QLoRA run.

Excluded by design:
  smoke_test  — synthetic fixtures, not real market decisions
  error rows  — an API/model failure is not a judgement
  replays     — decisions the old re-ask loop duplicated (replays > 1 keeps
                ONE row; the duplicates were already collapsed in the
                journal, so this only annotates)
"""

import argparse
import json
import sqlite3
import sys
from collections import Counter

import journal

EXCLUDED_SOURCES = {"smoke_test"}
OUT_FILE = "training_candidate.jsonl"


def _load(db_file: str) -> list:
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM decisions ORDER BY id").fetchall()
    grades = {}
    try:
        for g in conn.execute("SELECT * FROM intern_grades"):
            grades[(g["date"], g["ticker"])] = dict(g)
    except sqlite3.OperationalError:
        pass
    conn.close()
    return [dict(r) for r in rows], grades


def build_rows(decisions: list, grades: dict):
    """Returns (kept_rows, excluded_counter)."""
    kept, excluded = [], Counter()
    for d in decisions:
        source = d.get("source") or "unknown"
        try:
            verdict = json.loads(d.get("verdict") or "{}")
        except json.JSONDecodeError:
            excluded["unparseable_verdict"] += 1
            continue
        try:
            context = json.loads(d.get("context") or "{}")
        except json.JSONDecodeError:
            context = {}

        if source in EXCLUDED_SOURCES:
            excluded["smoke_test"] += 1
            continue
        if "error" in verdict:
            excluded["error_row"] += 1
            continue

        date = (d.get("timestamp") or "")[:10]
        grade = grades.get((date, d.get("ticker")))
        kept.append({
            "id": d["id"],
            "timestamp": d.get("timestamp"),
            "ticker": d.get("ticker"),
            "setup_name": d.get("setup_name"),
            "source": source,
            "inputs_json": context,
            "verdict": verdict,
            "conviction": d.get("conviction_score"),
            "reasoning": verdict.get("reasoning"),
            "grade": (grade or {}).get("grade"),
            "grade_note": (grade or {}).get("grade_note"),
            "outcome_pnl_usd": d.get("outcome_pnl_usd"),
            "outcome_pnl_pct": d.get("outcome_pnl_pct"),
            "outcome_linked": d.get("outcome_trade_id") is not None,
            "prompt_version": context.get("prompt_version"),
            "replays": d.get("replays") or 1,
            "tags": [t for t in [verdict.get("tag"),
                                 "approved" if d.get("approved") else "rejected"]
                     if t],
        })
    return kept, excluded


def audit(kept: list, excluded: Counter) -> str:
    lines = ["# Training-data audit", ""]
    lines.append(f"Exportable rows: **{len(kept)}**")
    lines.append(f"Excluded rows: **{sum(excluded.values())}**")
    for reason, n in excluded.most_common():
        lines.append(f"  - {reason}: {n}")

    by_source = Counter(r["source"] for r in kept)
    lines.append("\n## By source")
    for s, n in by_source.most_common():
        lines.append(f"- {s}: {n}")

    by_version = Counter(str(r["prompt_version"]) for r in kept)
    lines.append("\n## By prompt_version (None = pre-versioning)")
    for v, n in by_version.most_common():
        lines.append(f"- v{v}: {n}")

    graded = [r for r in kept if r["grade"]]
    linked = [r for r in kept if r["outcome_linked"]]
    with_reasoning = [r for r in kept if (r["reasoning"] or "").strip()]
    replayed = [r for r in kept if (r["replays"] or 1) > 1]

    def pct(part):
        return f"{100 * len(part) / len(kept):.1f}%" if kept else "n/a"

    lines.append("\n## Coverage")
    lines.append(f"- Graded by the CEO: **{len(graded)}** ({pct(graded)})")
    lines.append(f"- With a linked outcome (realized PnL): **{len(linked)}** "
                 f"({pct(linked)})")
    lines.append(f"- With non-empty reasoning: **{len(with_reasoning)}** "
                 f"({pct(with_reasoning)})")
    lines.append(f"- Rows that were replayed by the old re-ask loop "
                 f"(annotated, kept once): **{len(replayed)}**")

    lines.append("\n## What is missing for a supervised fine-tune")
    if kept:
        if len(graded) / len(kept) < 0.1:
            lines.append(f"- **Grades are the bottleneck**: only {len(graded)} "
                         f"rows carry a CEO grade. Preference/quality tuning "
                         f"needs far more; the grade-batch CLI exists for this.")
        if len(linked) / len(kept) < 0.1:
            lines.append(f"- **Outcomes are sparse**: {len(linked)} rows link to "
                         f"realized PnL. Outcome-weighted training needs closed "
                         f"trades, which accrue only as positions exit.")
        vmix = [v for v in by_version if v != "None"]
        if len(vmix) > 1:
            lines.append(f"- **Mixed prompt versions** ({', '.join('v' + v for v in sorted(vmix))}): "
                         f"conviction semantics changed between versions — "
                         f"segment or filter before training.")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default=OUT_FILE)
    p.add_argument("--db", default=journal.DB_FILE)
    args = p.parse_args()

    decisions, grades = _load(args.db)
    kept, excluded = build_rows(decisions, grades)
    with open(args.out, "w", encoding="utf-8") as f:
        for row in kept:
            f.write(json.dumps(row, default=str) + "\n")

    report = audit(kept, excluded)
    print(report)
    print(f"\nWritten: {args.out} ({len(kept)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
