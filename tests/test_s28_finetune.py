"""S28 item 4 — fine-tune prep: guards, plan and export shape (no training here)."""

import datetime as dt
import json
import os
from zoneinfo import ZoneInfo

import finetune_mlx_run as run
import finetune_plan

ET = ZoneInfo("America/New_York")


def at(s):
    return dt.datetime.fromisoformat(s).replace(tzinfo=ET)


def test_runs_only_before_the_morning_or_sunday_evening():
    assert run.deadline(at("2026-10-16 03:20")) == at("2026-10-16 08:45")   # Fri night
    assert run.deadline(at("2026-10-16 17:00")) == at("2026-10-18 20:00")   # Fri evening
    assert run.deadline(at("2026-10-17 10:00")) == at("2026-10-18 20:00")   # Saturday
    assert run.deadline(at("2026-10-13 20:00")) == at("2026-10-14 08:45")   # Tue evening


def test_refuses_while_the_worker_runs(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(run, "worker_running", lambda *a, **k: True)
    assert run.main() == 2 and "REFUSED" in capsys.readouterr().out


def test_refuses_a_weeknight_run_that_cannot_finish(monkeypatch, capsys):
    monkeypatch.setattr(run, "worker_running", lambda *a, **k: False)
    monkeypatch.setattr(run, "deadline", lambda now=None: dt.datetime.now(ET) + dt.timedelta(hours=5))
    assert run.main() == 2 and "weekend" in capsys.readouterr().out


def test_refuses_with_too_little_time_left(monkeypatch, capsys):
    monkeypatch.setattr(run, "worker_running", lambda *a, **k: False)
    monkeypatch.setattr(run, "deadline", lambda now=None: dt.datetime.now(ET) + dt.timedelta(minutes=10))
    assert run.main() == 2 and "45 min" in capsys.readouterr().out


def test_training_runs_in_the_separate_venv_on_the_s28_model():
    cmd = finetune_plan.training_command(68)
    assert "TradingBot-mlvenv" in cmd and "Qwen3.5-9B-4bit" in cmd
    assert "--mask-prompt" in cmd and "--iters 204" in cmd
    assert "--max-seq-length 4608" in cmd       # longest example is 4,183
    assert "TradingBot/venv" not in cmd


def test_export_rows_are_chat_examples_with_a_verdict_answer():
    path = "data/s28/finetune/train.jsonl"
    if not os.path.exists(path):
        return                                   # export not run on this machine
    rows = [json.loads(line) for line in open(path)]
    for r in rows:
        assert [m["role"] for m in r["messages"]] == ["system", "user", "assistant"]
        v = json.loads(r["messages"][2]["content"])
        assert "approved" in v and "conviction_score" in v
        assert r["meta"]["outcome_r"] is not None
    ts = [r["meta"]["ts"] for r in rows]
    assert ts == sorted(ts)                      # chronological
