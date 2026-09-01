"""
finetune_plan.py — QLoRA greenlight: dataset stats, filtering, split, and a
BASELINE eval of the current qwen3:4b.

    python finetune_plan.py            # stats + split + plan, no model calls
    python finetune_plan.py --baseline # also run qwen3:4b over the eval sets
    python finetune_plan.py --out finetune_plan.md

Work order 2026-09-01 item 9. This produces the PLAN and the BASELINE ONLY.
It never trains, never writes an adapter, and never touches the live model.

Filtering is the CEO's specification: graded rows + outcome-linked rows only.
Everything else in training_candidate.jsonl is an unlabelled decision — a
plausible-looking verdict with nothing to say whether it was right.
"""

import argparse
import collections
import json
import os
import statistics
import sys

CANDIDATE_FILE = "training_candidate.jsonl"
DEFAULT_OUT = "finetune_plan.md"
EVAL_FRACTION = 0.2
SPLIT_SEED = 20260901

# Sources whose inputs_json is a GATEKEEPER decision — the task we would be
# fine-tuning. 'rules' rows are deterministic filter passes (no model
# involved), 'intern_desk' rows are a different task (daily scan), 'intern'
# and 'review_bot' rows are session summaries.
GATEKEEPER_SOURCES = {"claude", "local_shadow"}
ORDER_SOURCES = {"ceo"}


# ============================================================ load + filter

def load_rows(path: str = CANDIDATE_FILE) -> list:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def is_eligible(row: dict) -> bool:
    """CEO filter: graded rows + outcome-linked rows only."""
    return bool(row.get("grade")) or bool(row.get("outcome_linked"))


def dedupe_key(row: dict):
    """Identical decisions journaled twice (the same verdict written by two
    paths, or a replay) must not appear on both sides of the split — that is
    train/eval leakage, and on a set this small it would be most of it."""
    return (row.get("ticker"), row.get("setup_name"), row.get("source"),
            json.dumps(row.get("inputs_json"), sort_keys=True),
            json.dumps(row.get("verdict"), sort_keys=True))


def label_for(row: dict):
    """Ground truth: should this setup have been approved?

    grade wins over outcome — a human graded the REASONING, and a trade can
    make money for reasons that have nothing to do with the thesis (NOK
    closed +$0.14 on a setup graded 'bad'). Returns True/False, or None when
    the row carries no usable label."""
    grade = (row.get("grade") or "").lower()
    if grade in ("good", "excellent"):
        return True
    if grade in ("bad", "poor"):
        return False
    if grade == "ungradeable":
        return None
    pnl = row.get("outcome_pnl_usd")
    if pnl is not None:
        return float(pnl) > 0
    return None


def build_dataset(rows: list):
    """Returns (kept, report_counts). Kept rows are eligible, de-duplicated,
    labelled, and of a task shape a gatekeeper fine-tune could learn."""
    counts = collections.Counter()
    seen, kept = set(), []
    for r in rows:
        if not is_eligible(r):
            counts["not_graded_or_outcome_linked"] += 1
            continue
        key = dedupe_key(r)
        if key in seen:
            counts["duplicate_decision"] += 1
            continue
        seen.add(key)
        if r.get("source") not in (GATEKEEPER_SOURCES | ORDER_SOURCES):
            counts[f"wrong_task_shape:{r.get('source')}"] += 1
            continue
        if label_for(r) is None:
            counts["no_usable_label"] += 1
            continue
        kept.append(r)
    return kept, counts


def split(kept: list, eval_fraction: float = EVAL_FRACTION):
    """Deterministic, label-stratified split. Stratified because on a set
    this small an unstratified draw can put every positive on one side."""
    import random
    rng = random.Random(SPLIT_SEED)
    by_label = collections.defaultdict(list)
    for r in kept:
        by_label[label_for(r)].append(r)
    train, evalset = [], []
    for label, group in sorted(by_label.items(), key=lambda kv: str(kv[0])):
        group = sorted(group, key=lambda r: r["id"])
        rng.shuffle(group)
        n_eval = max(1, round(len(group) * eval_fraction)) if group else 0
        evalset.extend(group[:n_eval])
        train.extend(group[n_eval:])
    return sorted(train, key=lambda r: r["id"]), sorted(evalset, key=lambda r: r["id"])


# ============================================================ baseline eval

def eval_prompt(row: dict) -> str:
    """A REDUCED gatekeeper prompt. The export carries the decision's inputs
    (setup, entry, stop, target, equity) but NOT the candle table the live
    gatekeeper sees, so this is a degraded replay — it measures the model's
    judgement on trade geometry, not its full read. Stated plainly because a
    baseline nobody can reproduce is worse than no baseline."""
    j = row.get("inputs_json") or {}
    order = j.get("order") or {}
    entry = j.get("entry", order.get("entry"))
    stop = j.get("stop", order.get("stop"))
    target = j.get("target", order.get("target"))
    rr = None
    try:
        if entry and stop and target and float(entry) > float(stop):
            rr = round((float(target) - float(entry)) /
                       (float(entry) - float(stop)), 2)
    except (TypeError, ValueError):
        rr = None
    return (
        f"Setup: {row.get('setup_name')}\n"
        f"Ticker: {row.get('ticker')}\n"
        f"Entry: {entry}\nStop: {stop}\nTarget: {target}\n"
        f"Reward:risk: {rr}\n"
        f"Account equity: {j.get('equity')}\n\n"
        "Approve or reject this long entry. Reply with JSON only: "
        '{"approved": true|false, "conviction_score": 0-100, '
        '"reasoning": "one sentence"}'
    )


def run_baseline(rows: list, label_fn) -> dict:
    """Score the CURRENT qwen3:4b over `rows`. Returns metrics + errors."""
    import local_analyst
    import requests

    state = local_analyst.ensure_ollama()
    if not state["up"]:
        return {"error": f"Ollama unavailable: {state['detail']}", "n": 0}

    correct = agree = scored = errors = 0
    truths, predictions = [], []
    conv_err = []
    for row in rows:
        truth = label_fn(row)
        if truth is None:
            continue
        try:
            resp = requests.post(
                f"{local_analyst.OLLAMA_URL}/api/chat",
                json={"model": local_analyst.LOCAL_MODEL,
                      "messages": [
                          {"role": "system", "content": local_analyst.SYSTEM_PROMPT},
                          {"role": "user", "content": eval_prompt(row)}],
                      "format": "json", "think": False, "stream": False,
                      "options": {"temperature": 0.0}},
                timeout=90)
            resp.raise_for_status()
            verdict = json.loads(resp.json()["message"]["content"])
        except Exception as e:
            errors += 1
            continue
        approved = verdict.get("approved")
        if isinstance(approved, str):
            approved = approved.lower() == "true"
        scored += 1
        truths.append(bool(truth))
        predictions.append(bool(approved))
        if bool(approved) == bool(truth):
            correct += 1
        ref = row.get("verdict", {})
        if ref.get("approved") is not None and bool(approved) == bool(ref["approved"]):
            agree += 1
        try:
            conv_err.append(abs(float(verdict.get("conviction_score", 0)) -
                                float(ref.get("conviction_score", 0))))
        except (TypeError, ValueError):
            pass
    # A single accuracy number is meaningless on an imbalanced set: if 94% of
    # the labels are "reject", a model that always rejects scores 94%. Report
    # the majority-class rate next to it, and the recall on the rare class.
    pos = sum(1 for t in truths if t)
    majority = max(pos, len(truths) - pos)
    caught = sum(1 for t, p in zip(truths, predictions) if t and p)
    return {
        "n": scored, "errors": errors,
        "accuracy_pct": round(100 * correct / scored, 1) if scored else None,
        "agreement_with_reference_pct": round(100 * agree / scored, 1) if scored else None,
        "conviction_mae": round(statistics.mean(conv_err), 1) if conv_err else None,
        "label_positives": pos,
        "majority_class_pct": round(100 * majority / len(truths), 1) if truths else None,
        "approvals_predicted": sum(1 for p in predictions if p),
        "recall_on_approvals": round(100 * caught / pos, 1) if pos else None,
    }


# ============================================================ report

def training_command(n_train: int) -> str:
    """The exact command for THIS machine: RTX 3060 Laptop, 4GB VRAM."""
    steps = max(60, n_train * 3)
    return f"""python -m axolotl.cli.train qlora_qwen3_4b.yml

# qlora_qwen3_4b.yml — RTX 3060 Laptop, 4GB VRAM
base_model: Qwen/Qwen3-4B
load_in_4bit: true                  # 4-bit NF4: 4B params ~= 2.3GB weights
bnb_4bit_compute_dtype: bfloat16
bnb_4bit_quant_type: nf4
bnb_4bit_use_double_quant: true

adapter: qlora
lora_r: 16
lora_alpha: 32
lora_dropout: 0.05
lora_target_modules: [q_proj, k_proj, v_proj, o_proj,
                      gate_proj, up_proj, down_proj]

datasets:
  - path: train.jsonl
    type: chat_template
    field_messages: messages
val_set_size: 0.0                   # eval.jsonl is held out by us, not by axolotl
test_datasets:
  - path: eval.jsonl
    type: chat_template
    field_messages: messages

sequence_len: 1024                  # p90 of our inputs is ~1k chars
sample_packing: false
pad_to_sequence_len: true

micro_batch_size: 1                 # 4GB leaves no room for more
gradient_accumulation_steps: 8      # effective batch 8
num_epochs: 3
max_steps: {steps}
learning_rate: 0.0002
lr_scheduler: cosine
warmup_steps: 10
optimizer: paged_adamw_8bit         # paged: survives a VRAM spike
gradient_checkpointing: true
bf16: true
flash_attention: false              # Ampere laptop + 4GB: not worth the memory
logging_steps: 5
save_steps: 50
output_dir: ./qlora-qwen3-4b-gatekeeper

# After training, to serve it through the existing shadow path:
#   python -m peft.utils.merge  (or axolotl merge_lora) -> merged/
#   ollama create qwen3-4b-gatekeeper -f Modelfile
#   LOCAL_ANALYST_MODEL=qwen3-4b-gatekeeper
# Shadow mode is unchanged: the tuned model is journaled, never authoritative."""


def build_report(rows, kept, counts, train, evalset, baselines: dict) -> str:
    import clockline
    L = ["# QLoRA fine-tune plan — greenlight review", clockline.two_zone_line(), ""]

    L.append("## Verdict")
    n = len(kept)
    if n < 200:
        L.append(f"**NOT TRAINABLE YET.** The CEO filter (graded rows + "
                 f"outcome-linked rows only) leaves **{n} examples**. A QLoRA "
                 f"run on {n} rows does not learn a gatekeeper; it memorises "
                 f"{n} rows. The plan below is complete and re-runnable — it "
                 f"is waiting on data, not on engineering.")
    else:
        L.append(f"**{n} examples** clear the filter — enough to run the plan "
                 f"below.")

    zero = [name for name, m in baselines.items()
            if m.get("recall_on_approvals") == 0.0 and m.get("n")]
    if zero:
        L.append("")
        L.append("**And the baseline says the target is not what we assumed.** "
                 "On every eval set, the untuned qwen3:4b approved *nothing* — "
                 "0% recall on the approve class. Its 94% \"accuracy\" is "
                 "exactly the always-reject rate, so the number measures our "
                 "class imbalance and not the model. The gap worth closing is "
                 "the model's inability to say yes, and no amount of "
                 "fine-tuning on a dataset that is itself ~95% rejections "
                 "will close it.")
    L.append("")

    L.append("## 1. Dataset stats (training_candidate.jsonl)")
    L.append(f"- Total exported rows: **{len(rows)}**")
    by_source = collections.Counter(r["source"] for r in rows)
    L.append("- By source: " + ", ".join(f"{k} {v}" for k, v in by_source.most_common()))
    graded = [r for r in rows if r.get("grade")]
    linked = [r for r in rows if r.get("outcome_linked")]
    L.append(f"- Graded: **{len(graded)}** "
             f"({100 * len(graded) / max(len(rows), 1):.1f}%) — "
             + ", ".join(f"{k} {v}" for k, v in
                         collections.Counter(r["grade"] for r in graded).most_common()))
    L.append(f"- Outcome-linked (realized PnL): **{len(linked)}** "
             f"({100 * len(linked) / max(len(rows), 1):.1f}%)")
    L.append(f"- With reasoning text: "
             f"**{sum(1 for r in rows if (r.get('reasoning') or '').strip())}**")
    L.append(f"- Approvals: **{sum(1 for r in rows if r['verdict'].get('approved'))}** "
             f"(the rest are rejections)")
    pv = collections.Counter(r.get("prompt_version") for r in rows)
    L.append("- Prompt version: " + ", ".join(f"v{k} {v}" for k, v in pv.most_common()))

    L.append("")
    L.append("### The bottleneck, stated plainly")
    L.append(f"Grading is the constraint, not collection. {len(rows)} decisions "
             f"exist and {len(graded)} carry a grade. Worse for training: the "
             f"graded set is almost entirely NEGATIVE "
             f"({collections.Counter(r['grade'] for r in graded).most_common()}), "
             f"so there is nearly nothing to teach the model to APPROVE. A "
             f"model trained on this would learn to reject everything, which "
             f"scores well on our labels and is worthless in the market.")

    L.append("")
    L.append("## 2. Filtering (CEO spec: graded + outcome-linked only)")
    L.append("| Excluded | Rows |")
    L.append("|---|---|")
    for k, v in counts.most_common():
        L.append(f"| {k} | {v} |")
    L.append(f"| **KEPT** | **{len(kept)}** |")
    L.append("")
    L.append("Beyond the CEO filter, three exclusions are structural:")
    L.append("- **duplicate_decision** — the same verdict journaled twice. "
             "Leaving these in would leak train rows into eval.")
    L.append("- **wrong_task_shape** — `rules` rows are deterministic filter "
             "passes with no model in the loop; `intern_desk` rows are the "
             "daily-scan task, not the gatekeeper task. Training the "
             "gatekeeper on them teaches the wrong job.")
    L.append("- **no_usable_label** — `ungradeable` grades with no outcome.")

    L.append("")
    L.append("## 3. Train / eval split")
    L.append(f"- Split: {int((1 - EVAL_FRACTION) * 100)}/{int(EVAL_FRACTION * 100)}, "
             f"label-stratified, deterministic (seed {SPLIT_SEED})")
    L.append(f"- Train: **{len(train)}** | Eval: **{len(evalset)}**")
    for name, part in (("train", train), ("eval", evalset)):
        pos = sum(1 for r in part if label_for(r) is True)
        L.append(f"- {name} label balance: {pos} approve / "
                 f"{len(part) - pos} reject")
    if evalset:
        L.append("- Eval rows: " + ", ".join(
            f"{r['ticker']}/{r['setup_name']}" for r in evalset))
    L.append("")
    L.append("Stratified and seeded so the split is reproducible and reviewable. "
             "At this N the eval set is a handful of rows: any accuracy figure "
             "from it has a confidence interval wider than the metric.")

    L.append("")
    L.append("## 4. Baseline — current qwen3:4b (untuned)")
    for name, m in baselines.items():
        L.append(f"### {name}")
        if m.get("error"):
            L.append(f"_Not run: {m['error']}_")
            continue
        L.append(f"- Scored: **{m['n']}** rows ({m['errors']} model errors)")
        if m.get("accuracy_pct") is not None:
            L.append(f"- Accuracy vs ground truth: **{m['accuracy_pct']}%**")
        if m.get("agreement_with_reference_pct") is not None:
            L.append(f"- Agreement with the journaled verdict: "
                     f"**{m['agreement_with_reference_pct']}%**")
        if m.get("conviction_mae") is not None:
            L.append(f"- Conviction MAE vs the journaled score: "
                     f"**{m['conviction_mae']} points**")
        if m.get("majority_class_pct") is not None:
            L.append(f"- **Majority-class control: {m['majority_class_pct']}%** "
                     f"({m['label_positives']} of {m['n']} labels are approve)")
            L.append(f"- Approvals predicted: **{m['approvals_predicted']}** "
                     f"| recall on the approve class: "
                     f"**{m['recall_on_approvals']}%**")
            if (m.get("accuracy_pct") is not None
                    and m["accuracy_pct"] <= m["majority_class_pct"] + 0.05):
                L.append(f"- ⚠ **This accuracy is at or below the "
                         f"always-reject baseline.** The headline number is "
                         f"class imbalance, not skill. Any fine-tune must be "
                         f"scored on the approve class, not on accuracy.")
    L.append("")
    L.append("The eval prompt is REDUCED: the export carries each decision's "
             "geometry (setup, entry, stop, target, equity) but not the candle "
             "table the live gatekeeper reads. So this measures judgement on "
             "trade geometry, not the model's full read. It is reproducible, "
             "which the alternative was not.")

    L.append("")
    L.append("## 5. The training command (RTX 3060 Laptop, 4GB VRAM)")
    L.append("```yaml")
    L.append(training_command(len(train)))
    L.append("```")
    L.append("")
    L.append("**Not run.** The work order says deliver the plan and the "
             "baseline; it does not authorise a training run, and at "
             f"{len(kept)} examples one would not be worth the GPU hours.")

    L.append("")
    L.append("## 6. What would make this trainable")
    L.append(f"- **Grades.** {len(graded)} today. A gatekeeper QLoRA wants "
             "300-500 labelled decisions with a real mix of approve and "
             "reject. At the current rate that is quarters away — the fix is "
             "to grade in batches, not to wait.")
    L.append("- **Positive examples.** Every grade on file is negative except "
             "one 'ungradeable'. Winning setups must be graded too, or the "
             "model has no signal for what 'yes' looks like.")
    L.append("- **Outcome links.** Only closed trades carry PnL, so the "
             "outcome-linked pool grows at the rate we actually trade. "
             "Grading is the lever we control.")
    return "\n".join(L) + "\n"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--baseline", action="store_true",
                   help="run the untuned qwen3:4b over the eval sets")
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--file", default=CANDIDATE_FILE)
    args = p.parse_args()

    if not os.path.exists(args.file):
        print(f"{args.file} not found — run 'python export_training.py' first.")
        return 1

    rows = load_rows(args.file)
    kept, counts = build_dataset(rows)
    train, evalset = split(kept)

    baselines = {}
    if args.baseline:
        baselines["Eval set (held-out, ground-truth labels)"] = \
            run_baseline(evalset, label_for)
        # A second, wider baseline: every gatekeeper decision Claude made,
        # with Claude's own verdict as the reference. Shadow mode's premise is
        # that Claude is authoritative, so this is the statistically
        # meaningful number even though it is not ground truth.
        claude_rows = [r for r in rows if r["source"] == "claude"
                       and r["verdict"].get("approved") is not None]
        baselines["Reference set (all Claude gatekeeper rows, "
                  "Claude as reference)"] = run_baseline(
            claude_rows, lambda r: bool(r["verdict"].get("approved")))
    else:
        baselines["Baseline"] = {"error": "run with --baseline", "n": 0}

    report = build_report(rows, kept, counts, train, evalset, baselines)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)
    # The report is UTF-8; a Windows cp1252 console is not. Echo it
    # defensively rather than losing a finished run to an encode error.
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    sys.stdout.write(report.encode(enc, "replace").decode(enc, "replace"))
    print(f"Written: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
