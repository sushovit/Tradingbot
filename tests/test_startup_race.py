"""
W-startup-race (2026-09-21): the watchdog killed two freshly started workers.
No network.

THE INCIDENT. 19:24:59 and 19:30:01 Nepal, both logged as
`heartbeat stale (235498s > 300s)` and `(235799s > 300s)`. 235,000 seconds
is 2.7 days: the heartbeat being read was FRIDAY's. A worker takes about 90
seconds to reach its first cycle -- logging, a 147-name universe scan, the
Ollama and Anthropic pre-flights, the SPY regime read -- and nothing wrote a
status line before then. So the watchdog saw `lock exists` beside `heartbeat
ancient` and did exactly what it is built to do, to a worker that was doing
nothing wrong. Its relaunch inherited the same blind window, so the failure
repeated rather than resolved. Five worker processes started that morning.

BOTH SIDES ARE WRONG, so both are fixed:
  (1) the starting worker writes a heartbeat the instant it takes the lock,
      so the file can never describe a previous session; and
  (2) the watchdog treats a live owner PID beside a young lock file as a
      worker that is STARTING, not one that is wedged.

Either fix alone would have prevented this. Both are kept because they fail
in different directions: (1) does nothing if the status write fails, and (2)
does nothing if the lock file is missing or the PID is dead.
"""

import os
import time

import pytest

import run_worker
import watchdog


# ============================================ (1) the startup heartbeat

def test_a_starting_worker_writes_a_heartbeat_immediately(tmp_path):
    target = tmp_path / "bot_status.log"
    assert run_worker.write_startup_status(4242, status_file=str(target))
    body = target.read_text(encoding="utf-8")
    assert "Worker starting (PID 4242)" in body
    assert body.startswith("[")                 # the [ET timestamp] shape


def test_the_line_parses_the_way_floor_reads_status(tmp_path):
    """floor.py splits on '] ' to separate stamp from message. A line it
    cannot parse would show up as a blank entry on the floor report."""
    target = tmp_path / "bot_status.log"
    run_worker.write_startup_status(7, status_file=str(target))
    line = target.read_text(encoding="utf-8").splitlines()[0]
    stamp, message = line.split("] ", 1)
    assert stamp.startswith("[")
    assert len(stamp) == len("[2026-09-21 10:07:12")
    assert message.strip()


def test_it_replaces_a_stale_heartbeat_rather_than_appending(tmp_path):
    """The whole point: Friday's content must not survive into Monday's
    startup window, because that content is what the watchdog reads."""
    target = tmp_path / "bot_status.log"
    target.write_text("[2026-09-18 16:14:59] Cycle #412 | Monitoring Live",
                      encoding="utf-8")
    run_worker.write_startup_status(99, status_file=str(target))
    body = target.read_text(encoding="utf-8")
    assert "Cycle #412" not in body
    assert "PID 99" in body


def test_the_heartbeat_is_fresh_from_second_zero(tmp_path, monkeypatch):
    """Measured against the real reader: after the startup write, the age
    the watchdog would compute is seconds, not days."""
    target = tmp_path / "bot_status.log"
    run_worker.write_startup_status(1, status_file=str(target))
    monkeypatch.setattr(run_worker, "STATUS_FILE", str(target))
    age = run_worker.live_heartbeat_age()
    assert age is not None and age < 5


def test_it_defaults_to_this_process(tmp_path):
    target = tmp_path / "bot_status.log"
    run_worker.write_startup_status(status_file=str(target))
    assert f"PID {os.getpid()}" in target.read_text(encoding="utf-8")


def test_a_failed_status_write_never_stops_the_worker(tmp_path, capsys):
    """Fail-soft, deliberately: the watchdog's own grace covers the same
    window from the other side, so a worker that cannot write this line
    should still start."""
    unwritable = tmp_path / "no-such-dir" / "bot_status.log"
    assert run_worker.write_startup_status(1, status_file=str(unwritable)) \
        is False
    assert "could not write startup status" in capsys.readouterr().out


def test_the_write_is_atomic(tmp_path):
    """Via safe_io, like every other status write. A torn write here would
    be read by the watchdog as a corrupt heartbeat during the exact window
    this exists to protect. The 2026-07-29 crash left 1,284 NUL bytes in
    this file once already."""
    import inspect
    src = inspect.getsource(run_worker.write_startup_status)
    assert "safe_io.atomic_write_text" in src


def test_it_is_called_immediately_after_the_lock_is_taken():
    """Order is the fix. Written after the universe scan it would be useless,
    and written before write_lock it would advertise a desk nobody owns."""
    with open("run_worker.py", encoding="utf-8") as f:
        src = f.read()
    lock = src.index("    write_lock(os.getpid())")
    startup = src.index("write_startup_status(os.getpid())", lock)
    imported = src.index("import streamlit_app", startup)
    assert lock < startup < imported
    # and nothing slow in between
    between = src[lock:startup]
    assert "import streamlit_app" not in between
    assert len(between.splitlines()) < 8


# ============================================ (2) the watchdog grace

def test_the_incident_as_it_happened():
    """Live-start, Friday heartbeat. This is the call that killed two
    workers, and it must now read as startup."""
    restart, reason = watchdog.needs_restart(
        True, 235498, lock_pid_alive=True, lock_age_secs=20)
    assert restart is False
    assert "starting up" in reason


def test_the_grace_expires_and_a_wedged_worker_is_still_killed():
    """The exemption is bounded. A live PID is not a licence to hang: past
    the window, a stale heartbeat restarts the worker as it always did."""
    restart, reason = watchdog.needs_restart(
        True, 235498, lock_pid_alive=True, lock_age_secs=400)
    assert restart is True
    assert "stale" in reason


def test_the_boundary_is_the_grace_window():
    assert watchdog.needs_restart(True, 9999, lock_pid_alive=True,
                                  lock_age_secs=299)[0] is False
    assert watchdog.needs_restart(True, 9999, lock_pid_alive=True,
                                  lock_age_secs=300)[0] is True


def test_a_dead_pid_gets_no_grace():
    """A young lock beside a DEAD owner is a crashed start, which is exactly
    when the watchdog should act fastest."""
    restart, _ = watchdog.needs_restart(True, 235498, lock_pid_alive=False,
                                        lock_age_secs=20)
    assert restart is True


def test_both_conditions_are_required():
    for alive, lock_secs in ((True, None), (False, 20), (None, 20),
                             (True, 9999)):
        assert watchdog.needs_restart(True, 235498, lock_pid_alive=alive,
                                      lock_age_secs=lock_secs)[0] is True


def test_no_status_file_at_all_is_covered_by_the_grace():
    """A worker one second old has not written anything yet. Before the fix
    this branch returned restart=True on its own."""
    assert watchdog.needs_restart(True, None, lock_pid_alive=True,
                                  lock_age_secs=1)[0] is False
    assert watchdog.needs_restart(True, None, lock_pid_alive=True,
                                  lock_age_secs=999)[0] is True


def test_a_deliberately_stopped_desk_still_outranks_everything():
    """No lock means the owner stopped it. That must remain the first test,
    or the watchdog resurrects a desk after the 16:15 shutdown."""
    restart, reason = watchdog.needs_restart(False, None, lock_pid_alive=True,
                                             lock_age_secs=1)
    assert restart is False
    assert "stopped deliberately" in reason


def test_a_healthy_worker_is_unchanged():
    restart, reason = watchdog.needs_restart(True, 29, lock_pid_alive=True,
                                             lock_age_secs=9999)
    assert restart is False
    assert "healthy" in reason


def test_the_old_signature_still_behaves_as_it_did():
    """Callers that pass no PID information keep the previous semantics —
    the new arguments default to None and grant no grace."""
    assert watchdog.needs_restart(True, 600)[0] is True
    assert watchdog.needs_restart(True, 45)[0] is False
    assert watchdog.needs_restart(True, None)[0] is True
    assert watchdog.needs_restart(False, 99999)[0] is False


# ============================================ lock_age

def test_lock_age_reads_the_lock_file(tmp_path):
    lock = tmp_path / "bot.run"
    lock.write_text("1234")
    age = watchdog.lock_age(str(lock))
    assert age is not None and age < 5


def test_lock_age_is_none_without_a_lock(tmp_path):
    assert watchdog.lock_age(str(tmp_path / "absent.run")) is None


def test_lock_age_tracks_process_start_not_activity(tmp_path):
    """The grace window only works because write_lock() is called ONCE and
    never refreshed — if anything touched the lock per cycle, the exemption
    would never expire and a hung worker would never be restarted."""
    with open("run_worker.py", encoding="utf-8") as f:
        src = f.read()
    assert src.count("    write_lock(os.getpid())") == 1
    import inspect
    import streamlit_app
    body = inspect.getsource(streamlit_app.live_bot_worker)
    # The loop may REMOVE the lock (shutdown) but must never rewrite it,
    # which would refresh the mtime the grace window is measured from.
    assert "atomic_write_text(LOCK_FILE" not in body
    assert "write_lock" not in body


def test_the_dashboard_start_path_has_no_startup_heartbeat():
    """Known gap, recorded rather than silently assumed away. Starting the
    bot from the Streamlit dashboard writes the lock directly and spawns a
    thread -- it never goes through run_worker.main(), so fix (1) does not
    reach it. Fix (2) does: the watchdog grants the grace on a live owner
    PID and a young lock, whoever wrote them. This is the clearest argument
    for keeping both fixes rather than picking one."""
    with open("streamlit_app.py", encoding="utf-8") as f:
        src = f.read()
    assert "safe_io.atomic_write_text(LOCK_FILE, str(os.getpid()))" in src
    assert "write_startup_status" not in src        # still true today
    # The lock is written ONCE on that path too, so lock_age stays valid.
    assert src.count("atomic_write_text(LOCK_FILE") == 1


# ============================================ the watchdog wires it up

def test_the_watchdog_passes_the_pid_and_lock_age():
    with open("watchdog.py", encoding="utf-8") as f:
        src = f.read()
    call = src.index("restart, reason = needs_restart(")
    block = src[call:call + 300]
    assert "lock_pid_alive=owner_alive" in block
    assert "lock_age_secs=lock_secs" in block
    # and it decides liveness from the OS, not from the heartbeat
    assert "run_worker.pid_alive(owner)" in src


def test_the_watchdog_logs_what_it_decided_on():
    """The 09-21 log line said only `lock=True heartbeat_age=235498s`, which
    is why the cause took a morning to find. The PID and lock age are now on
    the same line."""
    with open("watchdog.py", encoding="utf-8") as f:
        src = f.read()
    idx = src.index('print(f"watchdog: lock=')
    line = src[idx:idx + 400]
    for field in ("lock_age=", "owner=", "alive=", "heartbeat_age="):
        assert field in line, field


def test_the_session_window_guard_still_comes_first():
    """Outside the session nothing should be running, and the grace must not
    have reordered that check ahead of it."""
    with open("watchdog.py", encoding="utf-8") as f:
        src = f.read()
    main = src.index("def main(")
    window = src.index("in_session_window", main)
    decision = src.index("restart, reason = needs_restart(", main)
    assert window < decision
