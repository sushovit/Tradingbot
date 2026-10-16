"""
finetune_mlx_eval.py — S28 item 4: score a local MLX model (optionally with a
LoRA adapter) on the fine-tune validation set. Runs in the SEPARATE mlx venv
(~/TradingBot-mlvenv) - never the live venv - and imports nothing from the
trading code.

    ~/TradingBot-mlvenv/bin/python finetune_mlx_eval.py \
        --model ~/TradingBot-models/Qwen3.5-9B-4bit [--adapter adapters/...] \
        --data data/s28/finetune/valid.jsonl --out data/s28/eval_base.json

Per example: the model answers the live shadow prompt (thinking off, as the
shadow runs); its verdict is compared with Claude's (the training label) and
with the outcome. Metrics: parse rate, agreement with Claude on
approve/reject (approved and conviction >= 70), conviction MAE vs Claude,
Spearman(conviction, outcome R), latency.
"""

import argparse
import json
import os
import re
import statistics
import time


def spearman(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 5:
        return None
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2.0
            i = j + 1
        return r
    a, b = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    ma, mb = statistics.mean(a), statistics.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5
    return num / den if den else None


def parse(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
    except ValueError:
        return None
    try:
        conv = float(v.get("conviction_score"))
    except (TypeError, ValueError):
        conv = None
    appr = v.get("approved")
    if isinstance(appr, str):
        appr = appr.lower() == "true"
    return {"approved": bool(appr) and (conv or 0) >= 70, "conviction": conv}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--adapter")
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-tokens", type=int, default=400)
    a = ap.parse_args()
    from mlx_lm import generate, load
    model, tok = load(os.path.expanduser(a.model),
                      adapter_path=a.adapter)
    rows = [json.loads(line) for line in open(a.data)]
    out = []
    for r in rows:
        msgs = r["messages"][:2]
        try:
            prompt = tok.apply_chat_template(msgs, add_generation_prompt=True,
                                             enable_thinking=False)
        except TypeError:
            prompt = tok.apply_chat_template(msgs, add_generation_prompt=True)
        t0 = time.monotonic()
        text = generate(model, tok, prompt=prompt, max_tokens=a.max_tokens)
        dt = time.monotonic() - t0
        label = json.loads(r["messages"][2]["content"])
        out.append({"id": r["meta"]["decision_id"], "latency": dt,
                    "pred": parse(text), "outcome_r": r["meta"]["outcome_r"],
                    "claude_conv": label.get("conviction_score"),
                    "claude_approved": bool(label.get("approved"))
                    and (label.get("conviction_score") or 0) >= 70})
    ok = [o for o in out if o["pred"] is not None]
    conv = [o["pred"]["conviction"] for o in ok if o["pred"]["conviction"] is not None]
    summary = {
        "n": len(out), "parsed": len(ok),
        "agree_with_claude_pct": (100.0 * sum(o["pred"]["approved"] == o["claude_approved"]
                                              for o in ok) / len(ok)) if ok else None,
        "approval_rate_pct": (100.0 * sum(o["pred"]["approved"] for o in ok) / len(ok)) if ok else None,
        "conviction_mae_vs_claude": (statistics.mean(
            abs(o["pred"]["conviction"] - o["claude_conv"]) for o in ok
            if o["pred"]["conviction"] is not None and o["claude_conv"] is not None)
            if ok else None),
        "conviction_stdev": statistics.pstdev(conv) if len(conv) > 1 else None,
        "spearman_vs_outcome": spearman([o["pred"]["conviction"] for o in ok],
                                        [o["outcome_r"] for o in ok]),
        "claude_spearman_vs_outcome": spearman([o["claude_conv"] for o in out],
                                               [o["outcome_r"] for o in out]),
        "latency_median_s": statistics.median(o["latency"] for o in out) if out else None,
    }
    json.dump({"summary": summary, "rows": out}, open(a.out, "w"), indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
