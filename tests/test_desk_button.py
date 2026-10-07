"""
S12b — the owner's Start / Status buttons (jobs/macos/desk_button.py).

Every refusal path is exercised with the broker and the process table
replaced, so nothing here calls Alpaca, launchctl, or starts a worker.
"""

import importlib.util
import os
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import run_worker

JOBS = os.path.join("jobs", "macos")
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="bash")
ET = ZoneInfo("America/New_York")

_spec = importlib.util.spec_from_file_location(
    "desk_button", os.path.join(JOBS, "desk_button.py"))
db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(db)

WEDNESDAY = datetime(2026, 10, 7, 9, 40, tzinfo=ET)
SATURDAY = datetime(2026, 10, 10, 9, 40, tzinfo=ET)
SUNDAY = datetime(2026, 10, 11, 9, 40, tzinfo=ET)


def never(*a, **k):
    raise AssertionError("must not be consulted")


def ok_refusal(**over):
    kw = dict(now_et=WEDNESDAY, is_open=lambda: True,
              worker_pids=lambda: [], installed=lambda: True)
    kw.update(over)
    return db.refusal(**kw)


# ================================================== refusals

@pytest.mark.parametrize("day", [SATURDAY, SUNDAY])
def test_refuses_on_a_weekend_without_asking_the_broker(day):
    reason = db.refusal(now_et=day, is_open=never, worker_pids=never,
                        installed=never)
    assert "weekend" in reason


def test_refuses_on_a_market_holiday():
    reason = ok_refusal(is_open=lambda: False, worker_pids=never,
                        installed=never)
    assert "not a trading day" in reason


def test_refuses_when_the_lock_owner_is_alive(monkeypatch):
    """running_owner_pid() is not None."""
    import watchdog
    monkeypatch.setattr(run_worker, "running_owner_pid", lambda: 4242)
    monkeypatch.setattr(watchdog, "find_worker_pids", lambda: [])
    reason = ok_refusal(worker_pids=db.running_worker_pids, installed=never)
    assert "already running" in reason and "4242" in reason


def test_refuses_when_a_worker_process_exists_without_a_lock(monkeypatch):
    """find_worker_pids() is non-empty even though the lock is gone."""
    import watchdog
    monkeypatch.setattr(run_worker, "running_owner_pid", lambda: None)
    monkeypatch.setattr(watchdog, "find_worker_pids", lambda: [555])
    reason = ok_refusal(worker_pids=db.running_worker_pids, installed=never)
    assert "already running" in reason and "555" in reason


def test_refuses_when_the_launchd_job_is_not_installed():
    reason = ok_refusal(installed=lambda: False)
    assert "not installed" in reason and "install.sh" in reason


def test_no_refusal_on_a_normal_trading_day():
    assert ok_refusal() is None


def test_a_refusal_never_kickstarts():
    out = []
    rc = db.start(refuse=lambda: "weekend", launch=never, wait=never,
                  out=out.append)
    assert rc == 1
    assert out == ["NOT STARTED: weekend"]


def test_job_installed_asks_launchctl_for_the_start_job(monkeypatch):
    seen = {}

    def fake_run(cmd, **k):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 113)    # "not found"
    monkeypatch.setattr(db.subprocess, "run", fake_run)
    assert db.job_installed() is False
    assert seen["cmd"] == ["launchctl", "print",
                           f"gui/{os.getuid()}/com.tradingbot.start_worker"]


def test_a_broken_broker_is_treated_as_open(monkeypatch):
    """Fail-open, like the scheduled start: a false start is caught by
    session_clock, a false refusal loses a session."""
    import broker

    def boom():
        raise RuntimeError("no keys")
    monkeypatch.setattr(broker, "Broker", boom)
    assert db.market_open_today() is True


# ================================================== the start itself

def test_start_kickstarts_and_reports_the_pid():
    out, calls = [], []
    rc = db.start(refuse=lambda: None,
                  launch=lambda: calls.append("kick") or 0,
                  wait=lambda: 777, out=out.append)
    assert rc == 0 and calls == ["kick"]
    assert out[-1] == "Desk running, PID 777"


def test_start_shows_the_log_when_the_desk_does_not_come_up(tmp_path,
                                                            monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "start_worker.log").write_text(
        "".join(f"line {i}\n" for i in range(30)))
    out = []
    rc = db.start(refuse=lambda: None, launch=lambda: 0,
                  wait=lambda: None, out=out.append)
    assert rc == 1
    text = "\n".join(out)
    assert "did not come up within 120 s" in text
    assert "line 29" in text and "line 10" in text and "line 9\n" not in text


def test_start_shows_the_log_when_kickstart_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = []
    rc = db.start(refuse=lambda: None, launch=lambda: 3, wait=never,
                  out=out.append)
    assert rc == 1 and "kickstart failed (exit 3)" in "\n".join(out)


def test_the_kickstart_targets_the_scheduled_start_job(monkeypatch):
    seen = {}

    def fake_run(cmd, **k):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(db.subprocess, "run", fake_run)
    assert db.kickstart() == 0
    assert seen["cmd"] == ["launchctl", "kickstart",
                           f"gui/{os.getuid()}/com.tradingbot.start_worker"]


def test_waiting_needs_a_live_lock_owner_and_a_fresh_heartbeat(monkeypatch):
    states = iter([(None, False), (88, False), (88, True)])
    current = {}

    def owner():
        current["s"] = next(states)
        return current["s"][0]
    monkeypatch.setattr(run_worker, "running_owner_pid", owner)
    monkeypatch.setattr(run_worker, "another_worker_is_alive",
                        lambda: current["s"][1])
    assert db.wait_until_running(sleep=lambda s: None) == 88


def test_waiting_gives_up_after_the_deadline(monkeypatch):
    t = {"now": 0.0}
    monkeypatch.setattr(run_worker, "running_owner_pid", lambda: None)

    def sleep(s):
        t["now"] += s
    assert db.wait_until_running(wait_secs=120, poll=2.0, sleep=sleep,
                                 clock=lambda: t["now"]) is None
    assert t["now"] >= 120


# ================================================== status

def test_status_is_read_only_and_reports_everything(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "worker.log").write_text(
        "".join(f"w{i}\n" for i in range(15)))
    out = []
    rc = db.status(worker_pids=lambda: [], is_open=lambda: False,
                   positions=lambda: "## Open positions\n_None._",
                   out=out.append)
    text = "\n".join(out)
    assert rc == 0
    assert "Worker: not running" in text
    assert "Heartbeat: none" in text
    assert "ET): CLOSED" in text
    assert "## Open positions" in text
    assert "w14" in text and "w5" in text and "w4\n" not in text


def test_status_shows_the_pid_when_running(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = []
    db.status(worker_pids=lambda: [4242], is_open=lambda: True,
              positions=lambda: "", out=out.append)
    assert "Worker: PID 4242" in out
    assert any(l.endswith("ET): OPEN (trading day)") for l in out)


def test_status_never_starts_or_kickstarts():
    import inspect
    src = inspect.getsource(db.status)
    for verb in ("kickstart", "spawn_worker", "launchctl"):
        assert verb not in src


# ================================================== the Finder files

@pytest.mark.parametrize("name, mode", [("Start TradingBot.command", "start"),
                                        ("TradingBot Status.command", "status")])
def test_command_files_are_executable_wrappers(name, mode):
    path = os.path.join(JOBS, name)
    assert os.access(path, os.X_OK)
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert src.startswith("#!/bin/bash")
    assert 'realpath "$0"' in src                      # symlink-safe cd
    assert f"jobs/macos/desk_button.py {mode}" in src
    assert "read -r -p" in src                         # window waits
    assert "export TZ=America/New_York PYTHONUTF8=1" in src


@POSIX_ONLY
def test_a_desktop_symlink_still_finds_the_repo(tmp_path):
    """install.sh puts symlinks on the Desktop; the script must resolve them.
    Runs the status wrapper with a fake venv python that only prints cwd."""
    fake = tmp_path / "repo"
    (fake / "jobs" / "macos").mkdir(parents=True)
    (fake / "venv" / "bin").mkdir(parents=True)
    src = os.path.join(JOBS, "TradingBot Status.command")
    target = fake / "jobs" / "macos" / "TradingBot Status.command"
    target.write_text(open(src, encoding="utf-8").read())
    target.chmod(0o755)
    py = fake / "venv" / "bin" / "python"
    py.write_text("#!/bin/bash\npwd\n")
    py.chmod(0o755)
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    link = desktop / "TradingBot Status.command"
    link.symlink_to(target)
    r = subprocess.run(["bash", str(link)], input="\n", capture_output=True,
                       text=True, timeout=30)
    assert os.path.realpath(r.stdout.splitlines()[0]) == os.path.realpath(fake)


def test_install_puts_both_buttons_on_the_desktop():
    with open(os.path.join(JOBS, "install.sh"), encoding="utf-8") as f:
        src = f.read()
    assert '"Start TradingBot.command" "TradingBot Status.command"' in src
    assert 'ln -sfn "$HERE/$button" "$HOME/Desktop/$button"' in src
