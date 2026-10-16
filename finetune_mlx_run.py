"""
finetune_mlx_run.py — S28 item 4: run the mlx-lm LoRA fine-tune of the shadow
model, guarded. Shadow/offline only: it never touches the journal, the
config or the worker.

    python finetune_mlx_run.py            # checks, baseline eval, train, eval

Refuses to start unless:
  * the trading worker is not running (no bot.run, no run_worker.py), and
  * the run's hard deadline is in the future: 08:45 ET on a weekday (the
    09:20 shadow warm-up and the 09:25 worker need the memory back), or
    Sunday 20:00 ET at the weekend (CEO limit).
Ollama's model is unloaded first (training and the 7.7 GB shadow model do
not fit in 16 GB together); the 09:20 warm-up reloads it. Training is killed
at the deadline. Everything runs in ~/TradingBot-mlvenv.
"""

import datetime as dt
import json
import os
import subprocess
import sys
from zoneinfo import ZoneInfo

import finetune_plan

ET = ZoneInfo("America/New_York")
VENV_PY = os.path.expanduser(finetune_plan.MLX_VENV + "/bin/python")
MODEL = os.path.expanduser(finetune_plan.MLX_MODEL)
DATA = finetune_plan.S28_DATA
ADAPTER = "adapters/qwen3.5-9b-gatekeeper"
LOG = "data/s28/finetune_run.log"


def deadline(now=None):
    now = now or dt.datetime.now(ET)
    if now.weekday() < 5:
        d = now.replace(hour=8, minute=45, second=0, microsecond=0)
        if now >= d:          # after the morning cut: next allowed window
            d = d + dt.timedelta(days=1) if now.weekday() < 4 else \
                (now + dt.timedelta(days=6 - now.weekday())).replace(
                    hour=20, minute=0, second=0, microsecond=0)
        return d
    sunday = now + dt.timedelta(days=6 - now.weekday())
    return sunday.replace(hour=20, minute=0, second=0, microsecond=0)


def worker_running(repo="/Users/ranasaradb/TradingBot"):
    if os.path.exists(os.path.join(repo, "bot.run")):
        return True
    out = subprocess.run(["pgrep", "-f", "python run_worker.py"],
                         capture_output=True, text=True).stdout.strip()
    return bool(out)


def unload_ollama():
    import requests
    try:
        for m in requests.get("http://localhost:11434/api/ps", timeout=5).json().get("models", []):
            requests.post("http://localhost:11434/api/generate",
                          json={"model": m["name"], "keep_alive": 0}, timeout=60)
    except Exception as e:
        print(f"(ollama unload skipped: {e})")


def run(cmd, until):
    left = (until - dt.datetime.now(ET)).total_seconds()
    if left <= 60:
        raise TimeoutError("no time left before the deadline")
    with open(LOG, "a") as log:
        log.write(f"\n$ {' '.join(cmd)}\n")
        log.flush()
        p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        try:
            return p.wait(timeout=left)
        except subprocess.TimeoutExpired:
            p.kill()
            raise TimeoutError(f"killed at the {until:%a %H:%M %Z} deadline")


def main():
    now = dt.datetime.now(ET)
    until = deadline(now)
    if worker_running():
        print("REFUSED: the trading worker is running.")
        return 2
    if until - now < dt.timedelta(minutes=45):
        print(f"REFUSED: under 45 min before the {until:%a %H:%M %Z} deadline.")
        return 2
    if until - now < dt.timedelta(hours=6, minutes=30):
        print(f"REFUSED: the run needs ~6 h (measured: ~95 s per step); only "
              f"{(until - now).total_seconds() / 3600:.1f} h before the "
              f"{until:%a %H:%M %Z} deadline. Run it at the weekend.")
        return 2
    n_train = sum(1 for _ in open(os.path.join(DATA, "train.jsonl")))
    iters = max(60, n_train * 3)
    print(f"deadline {until:%a %Y-%m-%d %H:%M %Z}; {n_train} train rows, {iters} iters")
    unload_ollama()
    os.makedirs("data/s28", exist_ok=True)
    ev = [VENV_PY, "finetune_mlx_eval.py", "--model", MODEL,
          "--data", os.path.join(DATA, "valid.jsonl")]
    results = {}
    try:
        if not os.path.exists("data/s28/eval_base.json"):   # reuse a baseline
            run(ev + ["--out", "data/s28/eval_base.json"], until)
        t0 = dt.datetime.now(ET)
        rc = run([VENV_PY, "-m", "mlx_lm", "lora", "--model", MODEL, "--train",
                  "--data", DATA, "--fine-tune-type", "lora", "--num-layers", "16",
                  "--batch-size", "1", "--iters", str(iters),
                  "--learning-rate", "1e-5", "--max-seq-length", "4608",
                  "--grad-checkpoint", "--mask-prompt", "--steps-per-report", "10",
                  "--steps-per-eval", "102", "--val-batches", "6", "--save-every", "51",
                  "--adapter-path", ADAPTER], until)
        results["train_minutes"] = round((dt.datetime.now(ET) - t0).total_seconds() / 60, 1)
        results["train_rc"] = rc
        if rc == 0:
            run(ev + ["--adapter", ADAPTER, "--out", "data/s28/eval_tuned.json"], until)
    except TimeoutError as e:
        results["stopped"] = str(e)
    for name in ("base", "tuned"):
        p = f"data/s28/eval_{name}.json"
        if os.path.exists(p):
            results[name] = json.load(open(p))["summary"]
    json.dump(results, open("data/s28/finetune_result.json", "w"), indent=1)
    print(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
