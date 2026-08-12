"""
Single-instance enforcement (priority override 2026-08-10). No network.

  1. a second launch against a FRESH heartbeat exits non-zero
  2. the lock file carries the owning PID; takeover kills that PID
  3. status/heartbeat writes are atomic
"""

import os
import time

import pytest

import run_worker
import safe_io


# ------------------------------------------------------------------ 1

def test_second_launch_exits_nonzero(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot_status.log").write_text("[now] Cycle #9")   # fresh
    (tmp_path / "bot.run").write_text("4242")

    started = []
    monkeypatch.setattr(run_worker, "write_lock",
                        lambda *a, **k: started.append("lock"))

    rc = run_worker.main([])
    assert rc != 0                                   # non-zero exit
    assert rc == 1
    out = capsys.readouterr().out
    assert "worker already running" in out
    assert "refusing to start" in out
    assert "4242" in out                             # names the owner PID
    assert started == []                             # never claimed the lock


def test_stale_heartbeat_allows_start(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    status = tmp_path / "bot_status.log"
    status.write_text("[old] Cycle #1")
    old = time.time() - 3600
    os.utime(status, (old, old))
    assert run_worker.another_worker_is_alive() is False


def test_no_status_file_allows_start(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run_worker.another_worker_is_alive() is False


# ------------------------------------------------------------------ 2

def test_lock_records_pid_roundtrip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_worker.write_lock(31337)
    assert run_worker.read_lock_pid() == 31337
    assert (tmp_path / "bot.run").read_text().strip() == "31337"


def test_read_lock_pid_tolerates_legacy_empty_lock(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot.run").write_text("")            # pre-PID lock file
    assert run_worker.read_lock_pid() is None
    (tmp_path / "bot.run").write_text("not-a-pid")
    assert run_worker.read_lock_pid() is None


def test_takeover_kills_recorded_pid(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot_status.log").write_text("[now] Cycle #9")   # fresh
    (tmp_path / "bot.run").write_text("777")

    killed, locked = [], []
    monkeypatch.setattr(run_worker, "kill_pid",
                        lambda pid, **k: killed.append(pid) or True)
    monkeypatch.setattr(run_worker, "write_lock",
                        lambda *a, **k: locked.append(a))

    # Stop before importing streamlit_app: the takeover path is what matters.
    class Boom(Exception):
        pass

    def fake_import(name, *a, **k):
        raise Boom()
    monkeypatch.setattr("builtins.__import__", fake_import)
    with pytest.raises(Boom):
        run_worker.main(["--force-takeover"])

    assert killed == [777]                           # killed the RECORDED pid
    assert locked                                    # then claimed the lock


def test_takeover_aborts_if_owner_survives(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot_status.log").write_text("[now] Cycle #9")
    (tmp_path / "bot.run").write_text("888")

    monkeypatch.setattr(run_worker, "kill_pid", lambda pid, **k: False)
    claimed = []
    monkeypatch.setattr(run_worker, "write_lock",
                        lambda *a, **k: claimed.append(a))

    rc = run_worker.main(["--force-takeover"])
    assert rc == 2                                   # non-zero, distinct code
    assert claimed == []                             # never started beside it
    assert "could not kill owner" in capsys.readouterr().out


def test_takeover_with_legacy_lock_scans_and_kills(tmp_path, monkeypatch):
    """A PID-less lock must NOT let takeover proceed blindly — it falls back
    to the process scan, or aborts if a survivor cannot be killed."""
    import watchdog
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot_status.log").write_text("[now] Cycle #9")
    (tmp_path / "bot.run").write_text("")            # legacy: no PID

    killed = []
    # The scan also returns OUR own pid and our parent's — a launch creates a
    # matching parent+child pair. Neither may be killed (observed live: the
    # takeover killed itself).
    monkeypatch.setattr(watchdog, "find_worker_pids",
                        lambda: [555, os.getpid(), 556, os.getppid()])
    monkeypatch.setattr(run_worker, "kill_pid",
                        lambda pid, **k: killed.append(pid) or True)
    monkeypatch.setattr(run_worker, "write_lock", lambda *a, **k: None)

    class Boom(Exception):
        pass
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def fake_import(name, *a, **k):
        if name == "streamlit_app":
            raise Boom()
        return real_import(name, *a, **k)
    monkeypatch.setattr("builtins.__import__", fake_import)
    with pytest.raises(Boom):
        run_worker.main(["--force-takeover"])
    assert sorted(killed) == [555, 556]              # scanned and killed both


def test_watchdog_targets_the_recorded_pid(tmp_path, monkeypatch):
    import watchdog
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot.run").write_text("999")
    killed = []
    monkeypatch.setattr(run_worker, "kill_pid",
                        lambda pid, **k: killed.append(pid) or True)
    monkeypatch.setattr(watchdog, "find_worker_pids", lambda: [])
    n = watchdog.kill_stale_workers()
    assert killed == [999]                           # by PID, not by guessing
    assert n == 1


# ------------------------------------------------------------------ 3

def test_status_writes_are_atomic(tmp_path):
    target = tmp_path / "bot_status.log"
    safe_io.atomic_write_text(str(target), "[10:00] Cycle #1")
    assert target.read_text(encoding="utf-8") == "[10:00] Cycle #1"
    assert not (tmp_path / "bot_status.log.tmp").exists()


def test_worker_status_write_uses_atomic_path():
    """The live worker must route status through safe_io, not open()."""
    import inspect
    import streamlit_app as app
    src = inspect.getsource(app.write_status)
    assert "safe_io.atomic_write_text" in src
    assert 'open(STATUS_FILE, "w"' not in src
    src_pos = inspect.getsource(app.write_positions)
    assert "safe_io.atomic_write_text" in src_pos
