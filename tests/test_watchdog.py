"""
Worker-hang hardening (2026-07-29). No network, no process launches.

  - atomic status writes survive a simulated kill mid-write
  - watchdog restart decisions (pure logic) + mocked restart sequence
  - floor.py renders a NUL-corrupted status file without garbage
"""

import os

import pytest

import safe_io
import watchdog


# ------------------------------------------------------------ atomic writes

def test_atomic_write_replaces_only_on_success(tmp_path):
    target = tmp_path / "status.log"
    safe_io.atomic_write_text(str(target), "GOOD CONTENT")
    assert target.read_text(encoding="utf-8") == "GOOD CONTENT"
    assert not (tmp_path / "status.log.tmp").exists()   # tmp cleaned up


def test_simulated_kill_mid_write_leaves_old_file_intact(tmp_path, monkeypatch):
    """A process dying between the tmp write and os.replace must leave the
    ORIGINAL file untouched — never a half-written or NUL-filled target."""
    target = tmp_path / "status.log"
    safe_io.atomic_write_text(str(target), "ORIGINAL")

    def die(*a, **k):
        raise KeyboardInterrupt("simulated kill mid-write")
    monkeypatch.setattr(os, "replace", die)

    with pytest.raises(KeyboardInterrupt):
        safe_io.atomic_write_text(str(target), "NEW CONTENT THAT NEVER LANDS")

    assert target.read_text(encoding="utf-8") == "ORIGINAL"   # intact
    assert "\x00" not in target.read_text(encoding="utf-8")


def test_read_tolerant_strips_nulls(tmp_path):
    f = tmp_path / "status.log"
    f.write_bytes(b"[10:00] good line\n" + b"\x00" * 200 + b"\n[09:59] older\n")
    assert safe_io.is_corrupted(str(f)) is True
    out = safe_io.read_text_tolerant(str(f))
    assert "\x00" not in out
    assert out.splitlines() == ["[10:00] good line", "[09:59] older"]


def test_all_null_file_reads_as_empty(tmp_path):
    """The exact 2026-07-29 artefact: 1,284 bytes of pure NUL."""
    f = tmp_path / "status.log"
    f.write_bytes(b"\x00" * 1284)
    assert safe_io.is_corrupted(str(f)) is True
    assert safe_io.read_text_tolerant(str(f)) == ""


def test_floor_renders_corrupted_status_without_garbage(tmp_path, monkeypatch):
    import floor
    f = tmp_path / "status.log"
    f.write_bytes(b"\x00" * 1284)
    monkeypatch.setattr(floor, "STATUS_FILE", str(f))
    lines = floor.cycles_section()
    body = "\n".join(lines)
    assert "\x00" not in body
    assert "NUL bytes" in body               # the corruption is surfaced
    assert "No status history" in body


# ------------------------------------------------------------ watchdog logic

def test_no_lock_means_no_restart():
    restart, reason = watchdog.needs_restart(False, 99999)
    assert restart is False
    assert "stopped deliberately" in reason


def test_stale_heartbeat_with_lock_triggers_restart():
    restart, reason = watchdog.needs_restart(True, 600)
    assert restart is True
    assert "stale" in reason


def test_fresh_heartbeat_is_healthy():
    restart, reason = watchdog.needs_restart(True, 45)
    assert restart is False
    assert "healthy" in reason


def test_lock_without_status_file_triggers_restart():
    restart, reason = watchdog.needs_restart(True, None)
    assert restart is True
    assert "no status file" in reason


def test_boundary_exactly_at_threshold_is_healthy():
    assert watchdog.needs_restart(True, 300)[0] is False
    assert watchdog.needs_restart(True, 301)[0] is True


def test_restart_sequence_kills_clears_relaunches_alerts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot.run").write_text("")
    status = tmp_path / "bot_status.log"
    status.write_text("[old] stale")
    old = 1_600_000_000                          # ancient mtime
    os.utime(status, (old, old))

    actions = {"killed": 0, "launched": False, "alerts": []}
    monkeypatch.setattr(watchdog, "kill_stale_workers",
                        lambda: actions.__setitem__("killed", 2) or 2)
    # Kill confirmed: no survivors, so launching one instance is safe.
    monkeypatch.setattr(watchdog, "find_worker_pids", lambda: [])
    monkeypatch.setattr(watchdog, "relaunch_worker",
                        lambda: actions.__setitem__("launched", True) or True)
    monkeypatch.setattr(watchdog, "post_alert",
                        lambda m: actions["alerts"].append(m))

    assert watchdog.main() == 0
    assert actions["killed"] == 2
    assert actions["launched"] is True
    assert len(actions["alerts"]) == 1
    assert "restarted by watchdog" in actions["alerts"][0]
    assert not (tmp_path / "bot.run").exists()   # lock cleared for relaunch

    # Second run during the same outage must NOT re-alert (no flood).
    (tmp_path / "bot.run").write_text("")
    watchdog.main()
    assert len(actions["alerts"]) == 1


def test_healthy_run_takes_no_action(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot.run").write_text("")
    (tmp_path / "bot_status.log").write_text("[now] fresh")   # just written

    called = []
    monkeypatch.setattr(watchdog, "kill_stale_workers",
                        lambda: called.append("kill"))
    monkeypatch.setattr(watchdog, "relaunch_worker",
                        lambda: called.append("launch"))
    monkeypatch.setattr(watchdog, "post_alert", lambda m: called.append("alert"))

    assert watchdog.main() == 0
    assert called == []
    assert (tmp_path / "bot.run").exists()       # lock untouched
