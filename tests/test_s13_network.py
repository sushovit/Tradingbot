"""
S13 — every network call has a timeout, every scheduled job has a wall-clock
limit, and no log line carries a webhook URL or key. No real network: the
transport is stubbed at requests' adapter, which is where a timeout actually
takes effect.
"""

import io
import logging
import os
import subprocess
import sys
import textwrap
import time

import pytest
import requests

import net

JOBS = os.path.join(os.path.dirname(__file__), "..", "jobs")
sys.path.insert(0, JOBS)
import run_job  # noqa: E402

# A made-up webhook - never paste a real one here: this file is committed.
HOOK = ("https://discord.com/api/webhooks/100000000000000001/"
        "FAKEtoken0000-abcdefghijklmnopqrstuvwxyz_0123456789ABCDEFGH")
SECRET = "FAKEtoken0000"


@pytest.fixture
def sent(monkeypatch):
    """Capture (url, timeout) of every request at the adapter."""
    calls = []

    def send(self, request, **kwargs):
        calls.append((request.url, kwargs.get("timeout")))
        r = requests.Response()
        r.status_code = 200
        r._content = b'{"id": "x", "account_number": "1", "status": "ACTIVE"}'
        r.headers["Content-Type"] = "application/json"
        r.request = request
        r.url = request.url
        return r

    net.install(streams=False)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return calls


# ================================================== timeouts

def test_plain_requests_get_connect_5_read_30_unless_set(sent):
    requests.get("https://example.invalid/a")
    requests.get("https://example.invalid/b", timeout=3)
    assert sent[0][1] == (5, 30)
    assert sent[1][1] == 3                       # an explicit timeout wins


def test_alpaca_calls_carry_the_default_timeout(sent):
    from alpaca.trading.client import TradingClient
    client = TradingClient("PKTESTKEY000000000000", "secret", paper=True)
    try:
        client.get_account()
    except Exception:
        pass                                     # stub body; the send happened
    assert sent and all(t == (5, 30) for _, t in sent)
    assert "alpaca.markets" in sent[0][0]


def test_discord_webhooks_time_out_at_10s(sent):
    # conftest stubs execute() so no test can post; api_post_request() is
    # the library's real send path, and the adapter stub catches it here
    net.webhook(HOOK, content="x").api_post_request()
    assert sent[0][1] == 10
    assert net.webhook(HOOK, timeout=4).timeout == 4


def test_anthropic_client_has_a_60s_timeout(monkeypatch):
    import claude_integration
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-0000")
    monkeypatch.setattr(claude_integration, "_claude_client", None,
                        raising=False)
    client = claude_integration.get_claude_client() \
        if hasattr(claude_integration, "get_claude_client") else None
    if client is None:
        src = open("claude_integration.py", encoding="utf-8").read()
        assert "timeout=net.ANTHROPIC_TIMEOUT_S" in src
    else:
        assert client.timeout == 60


def test_no_raw_discord_webhook_left_in_the_codebase():
    here = os.path.join(os.path.dirname(__file__), "..")
    offenders = []
    for name in os.listdir(here):
        if name.endswith(".py") and name != "net.py":
            text = open(os.path.join(here, name), encoding="utf-8").read()
            if "DiscordWebhook(" in text:
                offenders.append(name)
    assert offenders == []


def test_yfinance_download_has_a_timeout():
    src = open("streamlit_app.py", encoding="utf-8").read()
    for line in src.splitlines():
        if "yf.download(" in line:
            assert "timeout=net.YFINANCE_TIMEOUT_S" in line


# ================================================== redaction

@pytest.mark.parametrize("raw, leaked", [
    (f"Discord notification failed: Max retries exceeded with url: {HOOK[19:]}"
     f" ({HOOK})", SECRET),
    ("key sk-ant-api03-abcdefghijklmnop rejected", "abcdefghijklmnop"),
    ("Authorization: Bearer abc.def.ghijklmnop", "ghijklmnop"),
    ("GET /v1?apikey=c8a1b2c3d4e5f6&x=1", "c8a1b2c3d4e5f6"),
    ("APCA-API-KEY-ID PKX2ABCDEFGHIJKLMNOP", "PKX2ABCDEFGHIJKLMNOP"),
])
def test_redact_masks_secrets(raw, leaked):
    out = net.redact(raw)
    assert leaked not in out and "redacted" in out


def test_redaction_keeps_the_discord_host_for_diagnosis():
    assert net.redact(HOOK) == "https://discord.com/api/webhooks/<redacted>"
    assert net.redact(None) is None and net.redact("plain text") == "plain text"


def test_every_log_record_is_redacted():
    net.install(streams=False)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    log = logging.getLogger("s13.child.logger")     # propagating child
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        log.warning("post failed: %s", HOOK)
        try:
            raise RuntimeError(f"boom {HOOK}")
        except RuntimeError:
            log.exception("with traceback")
    finally:
        log.removeHandler(handler)
    text = buf.getvalue()
    assert SECRET not in text and text.count("<redacted>") >= 2


def test_stdout_is_redacted_when_streams_are_wrapped():
    buf = io.StringIO()
    stream = net._RedactingStream(buf)
    print(f"(Discord notification failed: {HOOK})", file=stream)
    assert SECRET not in buf.getvalue()


# ================================================== wall-clock wrapper

def test_limits_per_job():
    assert run_job.limit_for("snapshot.py") == 600
    assert run_job.limit_for("drop.py") == 600
    for job in ("floor.py", "review_bot.py", "outcomes.py"):
        assert run_job.limit_for(job) == 900
    assert run_job.limit_for("intern_desk.py") == 1200
    assert run_job.limit_for("watchdog.py") == 900


def _script(tmp_path, body):
    path = tmp_path / "job.py"
    path.write_text(textwrap.dedent(body))
    return str(path)


def test_a_hung_job_is_killed_with_its_children_and_logged(tmp_path, capfd):
    pidfile = tmp_path / "child.pid"
    script = _script(tmp_path, f"""
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open({str(pidfile)!r}, "w").write(str(child.pid))
        time.sleep(60)
    """)
    start = time.time()
    rc = run_job.run([script], limit=2)
    assert rc == run_job.TIMEOUT_EXIT and time.time() - start < 20
    out = capfd.readouterr().out
    assert "TIMEOUT: job.py exceeded its 2s wall-clock limit" in out
    child = int(pidfile.read_text())
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)                        # grandchild died too


def test_a_job_that_ignores_sigterm_gets_sigkill(tmp_path, capfd):
    script = _script(tmp_path, """
        import signal, time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(60)
    """)
    rc = run_job.run([script], limit=1)
    assert rc == run_job.TIMEOUT_EXIT
    assert "killed (SIGKILL)" in capfd.readouterr().out


def test_a_normal_job_keeps_its_exit_code_and_runs_under_net(tmp_path, capfd):
    script = _script(tmp_path, f"""
        import sys, requests
        print("patched", getattr(requests.Session.request,
                                 "_s13_default_timeout", False))
        print("hook {HOOK}")
        sys.exit(3)
    """)
    assert run_job.run([script], limit=30) == 3
    out = capfd.readouterr().out
    assert "patched True" in out
    assert SECRET not in out and "webhooks/<redacted>" in out


def test_launchd_entry_uses_the_wrapper():
    sh = open(os.path.join(JOBS, "macos", "run_job.sh"), encoding="utf-8").read()
    assert "exec venv/bin/python jobs/run_job.py \"$@\"" in sh
