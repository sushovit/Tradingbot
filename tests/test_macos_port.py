"""
S12 — macOS port of the process helpers (2026-10-07). Windows unchanged.

The kill path is the dangerous one (HOSTING.md 1.3): a process-group kill
aimed at the wrong group takes down whatever launched the worker. These
tests use REAL processes, because a mocked os.killpg cannot show which
processes actually died.
"""

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

import local_analyst as la
import run_worker
import watchdog

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32",
                                reason="POSIX process groups")
DARWIN_ONLY = pytest.mark.skipif(sys.platform != "darwin", reason="macOS")

SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


def wait_dead(pid, secs=10):
    deadline = time.time() + secs
    while time.time() < deadline:
        if not run_worker.pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def reap():
    """Kill anything a test spawned, whatever the test did."""
    procs = []
    yield procs
    for p in procs:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            p.kill()
            p.wait(timeout=5)
        except Exception:
            pass


# ================================================================ pid_alive

@POSIX_ONLY
def test_pid_alive_sees_this_process():
    assert run_worker.pid_alive(os.getpid()) is True


@POSIX_ONLY
def test_pid_alive_reads_an_exited_child_as_dead():
    """An unreaped child is a zombie, and a zombie answers kill(pid, 0)."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    time.sleep(0.5)                       # exited, not yet reaped
    assert run_worker.pid_alive(p.pid) is False


@POSIX_ONLY
def test_pid_alive_treats_permission_error_as_alive(monkeypatch):
    def denied(pid, sig):
        raise PermissionError("another user's process")
    monkeypatch.setattr(run_worker.os, "kill", denied)
    assert run_worker.pid_alive(1) is True


@POSIX_ONLY
def test_pid_alive_on_a_missing_pid(monkeypatch):
    def gone(pid, sig):
        raise ProcessLookupError()
    monkeypatch.setattr(run_worker.os, "kill", gone)
    assert run_worker.pid_alive(999999) is False
    assert run_worker.pid_alive(None) is False


@POSIX_ONLY
def test_pid_alive_never_calls_tasklist(monkeypatch):
    calls = []
    real_run = subprocess.run

    def spy(cmd, *a, **k):
        calls.append(cmd[0])
        return real_run(cmd, *a, **k)
    monkeypatch.setattr(run_worker.subprocess, "run", spy)
    assert run_worker.pid_alive(os.getpid()) is True
    assert "tasklist" not in calls


@POSIX_ONLY
def test_pid_alive_reads_someone_elses_zombie_as_dead(reap, tmp_path):
    """A killed worker whose parent never reaps it is a zombie: dead."""
    pid_file = tmp_path / "z.pid"
    script = textwrap.dedent(f"""
        import os, subprocess, sys, time
        g = subprocess.Popen([sys.executable, "-c", "pass"])
        open({str(pid_file)!r}, "w").write(str(g.pid))
        time.sleep(60)          # never reaps g
    """)
    parent = subprocess.Popen([sys.executable, "-c", script],
                              start_new_session=True)
    reap.append(parent)
    deadline = time.time() + 10
    while not pid_file.exists() or not pid_file.read_text():
        assert time.time() < deadline
        time.sleep(0.05)
    assert wait_dead(int(pid_file.read_text()))


# ================================================================ kill_pid

@POSIX_ONLY
def test_kill_refuses_a_target_in_the_callers_own_group(reap):
    """THE safety test. A worker that is not its own group leader shares
    the launcher's group; killing that group would kill the caller."""
    p = subprocess.Popen(SLEEPER)                 # same group as pytest
    reap.append(p)
    assert os.getpgid(p.pid) == os.getpgrp()
    assert run_worker.kill_pid(p.pid, wait_secs=1) is False
    assert run_worker.pid_alive(p.pid) is True    # untouched
    assert run_worker.pid_alive(os.getpid()) is True


@POSIX_ONLY
def test_kill_takes_the_whole_worker_group(reap, tmp_path):
    """The worker leads its group; its children die with it (taskkill /T)."""
    child_pid_file = tmp_path / "grandchild.pid"
    script = textwrap.dedent(f"""
        import subprocess, sys, time
        g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open({str(child_pid_file)!r}, "w").write(str(g.pid))
        time.sleep(60)
    """)
    p = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    reap.append(p)
    deadline = time.time() + 10
    while not child_pid_file.exists() or not child_pid_file.read_text():
        assert time.time() < deadline, "grandchild never started"
        time.sleep(0.05)
    grandchild = int(child_pid_file.read_text())
    assert os.getpgid(p.pid) == p.pid != os.getpgrp()

    assert run_worker.kill_pid(p.pid, wait_secs=5) is True
    assert wait_dead(grandchild), "child of the worker survived the kill"
    assert run_worker.pid_alive(os.getpid()) is True


@POSIX_ONLY
def test_kill_escalates_to_sigkill(reap):
    stubborn = [sys.executable, "-c",
                "import signal, time; signal.signal(signal.SIGTERM, "
                "signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"]
    p = subprocess.Popen(stubborn, start_new_session=True,
                         stdout=subprocess.PIPE, text=True)
    reap.append(p)
    assert p.stdout.readline().strip() == "ready"   # TERM handler installed
    started = time.time()
    assert run_worker.kill_pid(p.pid, wait_secs=1) is True
    assert time.time() - started >= 1               # TERM was given its grace


@POSIX_ONLY
def test_a_non_leader_target_is_killed_alone(reap, tmp_path):
    """Started some other way, the target's group belongs to something
    else: signal the PID, never that group."""
    child_pid_file = tmp_path / "member.pid"
    script = textwrap.dedent(f"""
        import subprocess, sys, time
        g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open({str(child_pid_file)!r}, "w").write(str(g.pid))
        time.sleep(60)
    """)
    leader = subprocess.Popen([sys.executable, "-c", script],
                              start_new_session=True)
    reap.append(leader)
    deadline = time.time() + 10
    while not child_pid_file.exists() or not child_pid_file.read_text():
        assert time.time() < deadline
        time.sleep(0.05)
    member = int(child_pid_file.read_text())
    assert run_worker.kill_pid(member, wait_secs=5) is True
    assert wait_dead(member)
    assert run_worker.pid_alive(leader.pid) is True   # its group was spared


@POSIX_ONLY
def test_kill_of_a_vanished_pid_is_success(monkeypatch):
    def gone(pid):
        raise ProcessLookupError()
    monkeypatch.setattr(run_worker.os, "getpgid", gone)
    assert run_worker.kill_pid(999999) is True


# ================================================================ spawning

@POSIX_ONLY
def test_spawn_worker_makes_the_worker_a_group_leader(monkeypatch):
    seen = {}

    def fake_popen(args, **kw):
        seen["args"], seen["kw"] = args, kw
    monkeypatch.setattr(run_worker.subprocess, "Popen", fake_popen)
    run_worker.spawn_worker(["--force-takeover"], cwd="/x")
    assert seen["args"] == [sys.executable, "run_worker.py", "--force-takeover"]
    assert seen["kw"]["start_new_session"] is True
    assert seen["kw"]["cwd"] == "/x"


@POSIX_ONLY
def test_watchdog_relaunch_goes_through_spawn_worker(monkeypatch):
    seen = {}

    def fake_popen(args, **kw):
        seen["args"], seen["kw"] = args, kw
    monkeypatch.setattr(run_worker.subprocess, "Popen", fake_popen)
    assert watchdog.relaunch_worker() is True
    assert seen["args"][0] == sys.executable
    assert seen["args"][1:] == ["run_worker.py", "--force-takeover"]
    assert seen["kw"]["start_new_session"] is True


def test_watchdog_python_is_the_running_interpreter():
    assert watchdog.PYTHON == sys.executable


def test_the_intern_launch_uses_the_running_interpreter():
    with open("streamlit_app.py", encoding="utf-8") as f:
        src = f.read()
    assert 'Popen([sys.executable, "intern_desk.py"]' in src
    assert r"tradingbot\Scripts\python.exe" not in src


def test_the_start_script_spawns_through_spawn_worker():
    with open(os.path.join("jobs", "macos", "start_if_open.sh"),
              encoding="utf-8") as f:
        src = f.read()
    assert "run_worker.spawn_worker" in src
    assert "not a trading day - worker not started" in src


# ================================================================ pid scan

@POSIX_ONLY
def test_find_worker_pids_finds_a_worker_and_nothing_else(reap, tmp_path):
    fake = tmp_path / "run_worker.py"
    fake.write_text("import time; time.sleep(60)\n")
    worker = subprocess.Popen([sys.executable, str(fake)])
    # Lookalikes that must NOT match: a test file whose name merely ends in
    # run_worker.py, and a non-python process naming the script.
    test_file = tmp_path / "test_run_worker.py"
    test_file.write_text("import time; time.sleep(60)\n")
    lookalike = subprocess.Popen([sys.executable, str(test_file)])
    shell = subprocess.Popen(["/bin/sh", "-c", "sleep 60", "run_worker.py"])
    reap.extend([worker, lookalike, shell])
    time.sleep(0.3)

    pids = watchdog.find_worker_pids()
    assert worker.pid in pids
    assert lookalike.pid not in pids
    assert shell.pid not in pids
    assert os.getpid() not in pids


@POSIX_ONLY
def test_find_worker_pids_never_calls_wmic(monkeypatch):
    calls = []
    real_run = subprocess.run

    def spy(cmd, *a, **k):
        calls.append(cmd[0])
        return real_run(cmd, *a, **k)
    monkeypatch.setattr(watchdog.subprocess, "run", spy)
    watchdog.find_worker_pids()
    assert "wmic" not in calls and calls[0] == "pgrep"


@POSIX_ONLY
def test_the_sweep_kills_through_kill_pid_not_taskkill(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot.run").write_text("999")
    killed = []
    monkeypatch.setattr(run_worker, "kill_pid",
                        lambda pid, **k: killed.append(pid) or True)
    monkeypatch.setattr(watchdog, "find_worker_pids", lambda: [999, 1001])

    def no_taskkill(*a, **k):
        raise AssertionError("taskkill does not exist here")
    monkeypatch.setattr(watchdog.subprocess, "run", no_taskkill)
    assert watchdog.kill_stale_workers() == 2
    assert killed == [999, 1001]


# ================================================================ Ollama

@DARWIN_ONLY
@pytest.mark.parametrize("present, expected", [
    ({"/Applications/Ollama.app", "/opt/homebrew/bin/ollama"},
     ["open", "-a", "/Applications/Ollama.app"]),
    ({"/opt/homebrew/bin/ollama"}, ["/opt/homebrew/bin/ollama", "serve"]),
])
def test_ollama_is_started_from_the_mac_install(monkeypatch, present, expected):
    seen = {}
    ups = iter([False, True])
    monkeypatch.setattr(la, "is_up", lambda timeout=2.0: next(ups, True))
    monkeypatch.setattr(la.os.path, "exists", lambda p: p in present)
    monkeypatch.setattr(la.requests, "post", lambda *a, **k: None)
    monkeypatch.setattr(la.time, "sleep", lambda s: None)

    def fake_popen(args, **kw):
        seen["args"], seen["kw"] = args, kw
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    state = la.ensure_ollama()
    assert seen["args"] == expected
    assert seen["kw"].get("start_new_session") is True   # outlives the worker
    assert state["started"] is True and state["up"] is True


@DARWIN_ONLY
def test_ollama_missing_on_the_mac_is_fail_soft(monkeypatch):
    monkeypatch.setattr(la, "is_up", lambda timeout=2.0: False)
    monkeypatch.setattr(la.os.path, "exists", lambda p: False)

    def no_popen(*a, **k):
        raise AssertionError("nothing to launch")
    monkeypatch.setattr(subprocess, "Popen", no_popen)
    state = la.ensure_ollama(wait_secs=0)
    assert state["up"] is False and state["started"] is False
    assert "not found" in state["detail"]
    assert "/Applications/Ollama.app" in state["detail"]
