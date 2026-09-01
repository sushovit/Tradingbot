# QLoRA fine-tune plan — greenlight review
2026-08-31 22:15 ET  |  2026-09-01 08:00 Nepal  |  US market: CLOSED (opens Tue 19:15 Nepal)

## Verdict
**NOT TRAINABLE YET.** The CEO filter (graded rows + outcome-linked rows only) leaves **12 examples**. A QLoRA run on 12 rows does not learn a gatekeeper; it memorises 12 rows. The plan below is complete and re-runnable — it is waiting on data, not on engineering.

**And the baseline says the target is not what we assumed.** On every eval set, the untuned qwen3:4b approved *nothing* — 0% recall on the approve class. Its 94% "accuracy" is exactly the always-reject rate, so the number measures our class imbalance and not the model. The gap worth closing is the model's inability to say yes, and no amount of fine-tuning on a dataset that is itself ~95% rejections will close it.

## 1. Dataset stats (training_candidate.jsonl)
- Total exported rows: **2212**
- By source: intern_desk 994, rules 825, local_shadow 213, claude 126, intern 21, ceo 20, review_bot 13
- Graded: **12** (0.5%) — bad 11, ungradeable 1
- Outcome-linked (realized PnL): **10** (0.5%)
- With reasoning text: **1367**
- Approvals: **55** (the rest are rejections)
- Prompt version: vNone 1462, v3 315, v2 275, v4 160

### The bottleneck, stated plainly
Grading is the constraint, not collection. 2212 decisions exist and 12 carry a grade. Worse for training: the graded set is almost entirely NEGATIVE ([('bad', 11), ('ungradeable', 1)]), so there is nearly nothing to teach the model to APPROVE. A model trained on this would learn to reject everything, which scores well on our labels and is worthless in the market.

## 2. Filtering (CEO spec: graded + outcome-linked only)
| Excluded | Rows |
|---|---|
| not_graded_or_outcome_linked | 2192 |
| wrong_task_shape:intern_desk | 4 |
| wrong_task_shape:rules | 2 |
| duplicate_decision | 2 |
| **KEPT** | **12** |

Beyond the CEO filter, three exclusions are structural:
- **duplicate_decision** — the same verdict journaled twice. Leaving these in would leak train rows into eval.
- **wrong_task_shape** — `rules` rows are deterministic filter passes with no model in the loop; `intern_desk` rows are the daily-scan task, not the gatekeeper task. Training the gatekeeper on them teaches the wrong job.
- **no_usable_label** — `ungradeable` grades with no outcome.

## 3. Train / eval split
- Split: 80/20, label-stratified, deterministic (seed 20260901)
- Train: **9** | Eval: **3**
- train label balance: 0 approve / 9 reject
- eval label balance: 1 approve / 2 reject
- Eval rows: UBER/mean_reversion_reclaim, ORCL/mean_reversion_reclaim, NOK/mean_reversion_reclaim

Stratified and seeded so the split is reproducible and reviewable. At this N the eval set is a handful of rows: any accuracy figure from it has a confidence interval wider than the metric.

## 4. Baseline — current qwen3:4b (untuned)
### Eval set (held-out, ground-truth labels)
- Scored: **3** rows (0 model errors)
- Accuracy vs ground truth: **66.7%**
- Agreement with the journaled verdict: **33.3%**
- Conviction MAE vs the journaled score: **44.7 points**
- **Majority-class control: 66.7%** (1 of 3 labels are approve)
- Approvals predicted: **0** | recall on the approve class: **0.0%**
- ⚠ **This accuracy is at or below the always-reject baseline.** The headline number is class imbalance, not skill. Any fine-tune must be scored on the approve class, not on accuracy.
### Reference set (all Claude gatekeeper rows, Claude as reference)
- Scored: **126** rows (0 model errors)
- Accuracy vs ground truth: **94.4%**
- Agreement with the journaled verdict: **94.4%**
- Conviction MAE vs the journaled score: **9.0 points**
- **Majority-class control: 94.4%** (7 of 126 labels are approve)
- Approvals predicted: **0** | recall on the approve class: **0.0%**
- ⚠ **This accuracy is at or below the always-reject baseline.** The headline number is class imbalance, not skill. Any fine-tune must be scored on the approve class, not on accuracy.

The eval prompt is REDUCED: the export carries each decision's geometry (setup, entry, stop, target, equity) but not the candle table the live gatekeeper reads. So this measures judgement on trade geometry, not the model's full read. It is reproducible, which the alternative was not.

## 5. The training command (RTX 3060 Laptop, 4GB VRAM)
```yaml
python -m axolotl.cli.train qlora_qwen3_4b.yml

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
max_steps: 60
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
# Shadow mode is unchanged: the tuned model is journaled, never authoritative.
```

**Not run.** The work order says deliver the plan and the baseline; it does not authorise a training run, and at 12 examples one would not be worth the GPU hours.

## 6. What would make this trainable
- **Grades.** 12 today. A gatekeeper QLoRA wants 300-500 labelled decisions with a real mix of approve and reject. At the current rate that is quarters away — the fix is to grade in batches, not to wait.
- **Positive examples.** Every grade on file is negative except one 'ungradeable'. Winning setups must be graded too, or the model has no signal for what 'yes' looks like.
- **Outcome links.** Only closed trades carry PnL, so the outcome-linked pool grows at the rate we actually trade. Grading is the lever we control.
