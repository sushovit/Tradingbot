"""
S28 item 3 — shadow model upgrade + timeout fixes. Shadow only: nothing here
may reach a trading decision.
"""

import json
import os
import plistlib
import re

import analyst
import local_analyst

ROOT = os.path.join(os.path.dirname(__file__), "..")


class Resp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return {"message": {"content": json.dumps(self.payload)}}


def test_every_call_keeps_the_model_loaded_and_waits_45s(monkeypatch):
    seen = []
    monkeypatch.setattr(local_analyst.requests, "post",
                        lambda url, json=None, timeout=None: seen.append((json, timeout))
                        or Resp({"approved": False}))
    local_analyst._call_ollama("sys", "user", model="qwen3.5:9b")
    body, timeout = seen[0]
    assert body["keep_alive"] == -1 and body["model"] == "qwen3.5:9b"
    assert timeout == 45 and body["think"] is False
    assert body["options"]["num_ctx"] == 8192


def test_the_shadow_budget_is_45s():
    assert local_analyst.REQUEST_TIMEOUT == 45
    assert analyst.SHADOW_TIMEOUT_SECS == 45


def test_model_comes_from_config_unless_the_env_overrides(monkeypatch):
    monkeypatch.delenv("LOCAL_ANALYST_MODEL", raising=False)
    assert local_analyst.shadow_model({"models": {"shadow": "qwen3.5:9b"}}) == "qwen3.5:9b"
    assert local_analyst.shadow_model({}) == "qwen3:4b"
    monkeypatch.setenv("LOCAL_ANALYST_MODEL", "x:1b")
    assert local_analyst.shadow_model({"models": {"shadow": "qwen3.5:9b"}}) == "x:1b"
    cfg = json.load(open(os.path.join(ROOT, "bot_config.json"), encoding="utf-8"))
    assert cfg["models"]["shadow"]          # shipped, explicit


def test_warm_up_job_runs_at_0920_et_on_weekdays():
    d = plistlib.load(open(os.path.join(ROOT, "jobs", "macos",
                                        "com.tradingbot.shadow_warm.plist"), "rb"))
    assert d["Label"] == "com.tradingbot.shadow_warm"
    assert d["ProgramArguments"][-2:] == ["local_analyst.py", "--warm"]
    assert sorted((x["Weekday"], x["Hour"], x["Minute"])
                  for x in d["StartCalendarInterval"]) == \
        [(w, 9, 20) for w in range(1, 6)]
    src = open(os.path.join(ROOT, "local_analyst.py"), encoding="utf-8").read()
    assert '"--warm" in sys.argv' in src and '"keep_alive": KEEP_ALIVE' in src


def test_the_shadow_never_reaches_the_trading_path():
    """The shadow model stays an observer: no trading module imports it."""
    for name in ("risk.py", "orders.py", "position_mgmt.py", "daily_eval.py",
                 "claude_integration.py"):
        src = open(os.path.join(ROOT, name), encoding="utf-8").read()
        assert not re.search(r"^\s*(import local_analyst|from local_analyst import)",
                             src, re.M), name
    src = open(os.path.join(ROOT, "analyst.py"), encoding="utf-8").read()
    # in shadow mode the worker's verdict is Claude's; the local one is journaled only
    assert 'source="local_shadow"' in src


def test_shadow_calls_are_serialized(monkeypatch):
    """Concurrent shadow calls go to Ollama one at a time (bounded memory)."""
    import threading, time
    inflight, peak = [0], [0]
    def post(url, json=None, timeout=None):
        inflight[0] += 1; peak[0] = max(peak[0], inflight[0])
        time.sleep(0.05); inflight[0] -= 1
        return Resp({"approved": False})
    monkeypatch.setattr(local_analyst.requests, "post", post)
    ts = [threading.Thread(target=local_analyst._call_ollama, args=("s", "u")) for _ in range(5)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert peak[0] == 1


def test_the_shipped_shadow_is_the_s28_winner():
    cfg = json.load(open(os.path.join(ROOT, "bot_config.json"), encoding="utf-8"))
    assert cfg["models"]["shadow"] == "qwen3.5:9b"


def test_a_call_that_waited_out_its_budget_never_reaches_ollama(monkeypatch):
    import threading
    calls = []
    monkeypatch.setattr(local_analyst.requests, "post",
                        lambda *a, **k: calls.append(1) or Resp({}))
    local_analyst._OLLAMA_LOCK.acquire()            # a long call holds the slot
    try:
        err = []
        th = threading.Thread(target=lambda: err.append(
            _raises(lambda: local_analyst._call_ollama("s", "u", timeout=0.2))))
        th.start(); th.join(2)
    finally:
        local_analyst._OLLAMA_LOCK.release()
    assert err == ["Timeout"] and calls == []


def _raises(fn):
    try:
        fn()
    except Exception as e:
        return type(e).__name__
    return None
