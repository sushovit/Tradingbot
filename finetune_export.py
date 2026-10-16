"""
finetune_export.py — S28 item 4: export paired Claude gatekeeper decisions +
outcomes as mlx-lm LoRA chat data. EXPORT ONLY - nothing is journaled.

    python finetune_export.py --journal journal.db [--out data/s28/finetune]

Rows: every Claude gatekeeper decision (no error) whose prompt can be
rebuilt from bars (study_shadow_models.gk_frame, no lookahead, no news) AND
whose outcome is known (CEO filter: outcome-linked rows only) - realized R of
a filled trade, else the outcome tracker's resolved replay R.

Example = {"messages": [system (the junior-analyst gatekeeper prompt),
user (the rebuilt gatekeeper prompt), assistant (Claude's verdict JSON)]},
plus "meta" (decision id, setup, outcome R) that mlx-lm ignores. Split:
chronological 80/20 (train on the past, validate on the most recent) so the
validation set is a genuine forward test.
"""

import argparse
import json
import os
import pickle
import sqlite3

import prompts
import study_shadow_models as ssm

VERDICT_KEYS = ("approved", "conviction_score", "market_regime",
                "crossover_quality", "rejection_reason", "key_risk", "reasoning")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", default="journal.db")
    ap.add_argument("--out", default="data/s28/finetune")
    a = ap.parse_args()
    cfg = json.load(open("bot_config.json"))
    rp = cfg["risk_profiles"]["Moderate"]
    conn = sqlite3.connect(f"file:{a.journal}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    decs = [dict(r) for r in conn.execute(
        "SELECT * FROM decisions WHERE source='claude' AND "
        "json_extract(verdict,'$.error') IS NULL ORDER BY id")]
    daily = pickle.load(open(f"{ssm.CACHE}/daily_all.pkl", "rb"))
    five = pickle.load(open(f"{ssm.CACHE}/five_all.pkl", "rb"))
    system = prompts.get_system_prompt("junior_analyst")
    rows, skipped = [], {"no_outcome": 0, "no_bars": 0, "setup": 0}
    import local_analyst
    for d in decs:
        if d["setup_name"] not in ssm.LIVE_SETUPS:
            skipped["setup"] += 1
            continue
        r = ssm.outcome_r(conn, d["id"])
        if r is None:
            skipped["no_outcome"] += 1
            continue
        df = ssm.gk_frame(d, daily, five)
        if df is None:
            skipped["no_bars"] += 1
            continue
        kw = ssm.build_kwargs(d, df, rp)
        user = local_analyst.build_user_prompt(**kw)   # the live shadow prompt
        v = json.loads(d["verdict"] or "{}")
        target = {k: v.get(k) for k in VERDICT_KEYS}
        target["conviction_score"] = d["conviction_score"]
        rows.append({"messages": [{"role": "system", "content": system},
                                  {"role": "user", "content": user},
                                  {"role": "assistant", "content": json.dumps(target)}],
                     "meta": {"decision_id": d["id"], "setup": d["setup_name"],
                              "ts": d["timestamp"], "outcome_r": r}})
    os.makedirs(a.out, exist_ok=True)
    cut = int(len(rows) * 0.8)
    for name, part in (("train", rows[:cut]), ("valid", rows[cut:])):
        with open(os.path.join(a.out, f"{name}.jsonl"), "w", encoding="utf-8") as f:
            for row in part:
                f.write(json.dumps(row) + "\n")
    tok = [len(r["messages"][1]["content"]) // 4 for r in rows]
    print(f"exported {len(rows)} (train {cut}, valid {len(rows) - cut}); skipped {skipped}; "
          f"prompt ~{min(tok)}-{max(tok)} tokens, median {sorted(tok)[len(tok)//2]}")


if __name__ == "__main__":
    main()
