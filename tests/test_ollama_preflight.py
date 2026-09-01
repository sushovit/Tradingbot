"""
Ollama diagnosis + pre-flight (work order A4). No network.

Diagnosis from the journal: 30 shadow error rows, 27 of them CONNECTION
REFUSED, every one in the session's first hour (09:00-10:00 ET). Only 3
were read timeouts, and there were ZERO at the 15:30 intern run — so the
single-GPU collision hypothesis is not supported. The service simply was
not running when the desk started.
"""

import json

import pytest
import requests

import local_analyst as la


# ------------------------------------------------------ error classification

def test_connection_refused_is_named():
    err = requests.ConnectionError(
        "HTTPConnectionPool(host='localhost', port=11434): Max retries "
        "exceeded (Caused by NewConnectionError: [WinError 10061] ...refused)")
    assert la.classify_error(err) == "connection_refused"


def test_timeout_is_named_separately():
    assert la.classify_error(requests.Timeout("Read timed out.")) == "read_timeout"


def test_malformed_response_is_named():
    assert la.classify_error(json.JSONDecodeError("x", "{", 0)) == "malformed_response"
    assert la.classify_error(KeyError("message")) == "malformed_response"


def test_unknown_errors_are_not_mislabelled():
    assert la.classify_error(ValueError("something else")) == "unknown"


def test_journalled_error_carries_the_classification(monkeypatch):
    """The verdict's error string must say WHICH failure it was."""
    import pandas as pd
    monkeypatch.setattr(la, "_call_ollama", lambda *a, **k: (_ for _ in ()).throw(
        requests.ConnectionError("connection refused")))
    monkeypatch.setattr(la.time, "sleep", lambda s: None)
    rows = 20
    df20 = pd.DataFrame({"open": [1.0]*rows, "high": [1.0]*rows, "low": [1.0]*rows,
                         "close": [1.0]*rows, "volume": [1]*rows,
                         "ema_fast": [1.0]*rows, "ema_slow": [1.0]*rows,
                         "rsi_14": [50.0]*rows, "adx_14": [25.0]*rows})
    out = la.get_gatekeeper_decision(
        ticker="X", df20=df20, ema_spread_pct=0.1, volume_trend="flat",
        crossover_count=0, dist_to_resistance_pct=2.0, entry_price=1.0,
        stop_price=0.9, target_price=1.3, rr_ratio=3.0, interval_mins=5,
        fast_ema=9, slow_ema=21, news_headlines=[])
    assert "error" in out
    assert "connection_refused" in out["error"]      # not just "unreachable"


# ------------------------------------------------------------- pre-flight

def test_preflight_reports_ready_when_already_up(monkeypatch):
    monkeypatch.setattr(la, "is_up", lambda timeout=2.0: True)
    monkeypatch.setattr(la.requests, "post", lambda *a, **k: None)
    state = la.ensure_ollama()
    assert state["up"] is True
    assert state["started"] is False        # nothing needed starting
    assert state["warmed"] is True
    assert state["detail"] == "ready"


def test_preflight_starts_the_service_when_down(monkeypatch, tmp_path):
    calls = {"popen": 0}
    ups = iter([False, False, True])         # comes up on the second poll
    monkeypatch.setattr(la, "is_up", lambda timeout=2.0: next(ups, True))
    exe = tmp_path / "ollama app.exe"
    exe.write_text("")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(la.os.path, "join", lambda *a: str(exe))
    monkeypatch.setattr(la.os.path, "exists", lambda p: True)

    import subprocess
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *a, **k: calls.__setitem__("popen", 1))
    monkeypatch.setattr(la.time, "sleep", lambda s: None)
    monkeypatch.setattr(la.requests, "post", lambda *a, **k: None)

    state = la.ensure_ollama()
    assert calls["popen"] == 1               # tried to start it
    assert state["up"] is True and state["warmed"] is True


def test_preflight_never_raises_when_everything_fails(monkeypatch):
    monkeypatch.setattr(la, "is_up", lambda timeout=2.0: False)
    monkeypatch.setattr(la.os.path, "exists", lambda p: False)
    state = la.ensure_ollama(wait_secs=0)
    assert state["up"] is False
    assert "not found" in state["detail"] or state["detail"] == "DOWN"


def test_preflight_survives_a_failed_warmup(monkeypatch):
    monkeypatch.setattr(la, "is_up", lambda timeout=2.0: True)

    def boom(*a, **k):
        raise requests.Timeout("Read timed out.")
    monkeypatch.setattr(la.requests, "post", boom)
    state = la.ensure_ollama()
    assert state["up"] is True
    assert state["warmed"] is False
    assert "read_timeout" in state["detail"]
