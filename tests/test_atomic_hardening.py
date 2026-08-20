"""
Atomic-write hardening (2026-08-18). No network.

positions.json intermittently failed its atomic replace with WinError 2 —
the .tmp missing at rename, i.e. something (almost certainly AV) removed it
between write and rename. Retrying the rename cannot fix a vanished source.
"""

import os
import time

import pytest

import safe_io


# ------------------------------------------------------------ (a) unique tmp

def test_tmp_names_are_unique_per_call():
    a = safe_io._tmp_name("positions.json")
    b = safe_io._tmp_name("positions.json")
    assert a != b
    assert str(os.getpid()) in a
    assert a.startswith("positions.json.") and a.endswith(".tmp")


def test_no_tmp_left_behind_on_success(tmp_path):
    target = tmp_path / "positions.json"
    safe_io.atomic_write_text(str(target), '{"NOK": {}}')
    assert target.read_text(encoding="utf-8") == '{"NOK": {}}'
    assert [p.name for p in tmp_path.iterdir()] == ["positions.json"]


def test_orphaned_tmps_are_swept(tmp_path):
    target = tmp_path / "positions.json"
    target.write_text("{}")
    orphan = tmp_path / "positions.json.9999.123.tmp"
    orphan.write_text("junk")
    old = time.time() - 3600
    os.utime(orphan, (old, old))
    fresh = tmp_path / "positions.json.8888.456.tmp"      # another process, live
    fresh.write_text("in flight")

    safe_io.atomic_write_text(str(target), "new")
    assert not orphan.exists()          # stale leftover removed
    assert fresh.exists()               # someone else's in-flight temp kept


# ------------------------------------------------- (b) retries then fallback

def test_locked_destination_retries_then_succeeds(tmp_path, monkeypatch):
    target = tmp_path / "positions.json"
    target.write_text("OLD")
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError(13, "The process cannot access the file")
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", flaky)

    safe_io.atomic_write_text(str(target), "NEW")
    assert target.read_text(encoding="utf-8") == "NEW"
    assert calls["n"] == 3                     # retried, did not fall back


def test_backoff_is_50ms_and_capped_at_three_attempts(tmp_path, monkeypatch):
    target = tmp_path / "positions.json"
    target.write_text("OLD")
    sleeps, attempts = [], {"n": 0}

    def always_locked(src, dst):
        attempts["n"] += 1
        raise OSError(13, "locked")
    monkeypatch.setattr(os, "replace", always_locked)
    monkeypatch.setattr(safe_io.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(safe_io, "_journal_degradation", lambda *a: None)

    safe_io.atomic_write_text(str(target), "NEW")
    assert attempts["n"] == 3                  # 3 attempts, not 6
    assert sleeps[0] == pytest.approx(0.05)    # 50ms backoff
    assert target.read_text(encoding="utf-8") == "NEW"   # fallback still wrote


def test_vanished_tmp_rebuilds_instead_of_retrying(tmp_path, monkeypatch):
    """WinError 2: retrying the rename is useless, so the temp is rebuilt."""
    target = tmp_path / "positions.json"
    target.write_text("OLD")
    real_replace = os.replace
    calls = {"n": 0}

    def av_eats_tmp(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            os.remove(src)                     # scanner takes the temp file
            raise FileNotFoundError(2, "The system cannot find the file")
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", av_eats_tmp)

    safe_io.atomic_write_text(str(target), "NEW")
    assert target.read_text(encoding="utf-8") == "NEW"    # atomic after rebuild
    assert calls["n"] == 2                                # rebuilt once, no flailing


def test_persistent_vanishing_falls_back_and_writes(tmp_path, monkeypatch):
    target = tmp_path / "positions.json"
    target.write_text("OLD")
    calls = {"n": 0}

    def always_vanish(src, dst):
        calls["n"] += 1
        try:
            os.remove(src)
        except OSError:
            pass
        raise FileNotFoundError(2, "gone again")
    monkeypatch.setattr(os, "replace", always_vanish)
    logged = []
    monkeypatch.setattr(safe_io, "_journal_degradation",
                        lambda p, e, s: logged.append(s))

    safe_io.atomic_write_text(str(target), "NEW")
    assert target.read_text(encoding="utf-8") == "NEW"    # data never lost
    assert calls["n"] == 2                                # one rebuild, then stop
    assert logged == ["source vanished"]


# ------------------------------------------------------- (c) journaled

def test_fallback_is_journaled_for_the_reviewer(tmp_path, monkeypatch,
                                                temp_journal):
    import json
    import sqlite3
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "positions.json"
    target.write_text("OLD")
    monkeypatch.setattr(os, "replace",
                        lambda s, d: (_ for _ in ()).throw(OSError(13, "locked")))
    monkeypatch.setattr(safe_io.time, "sleep", lambda s: None)

    safe_io.atomic_write_text(str(target), "NEW")

    conn = sqlite3.connect(temp_journal.DB_FILE)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM decisions WHERE source='ops'").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["setup_name"] == "write_integrity_degraded"
    assert "positions.json" in json.loads(rows[0]["verdict"])["reasoning"]
    # The reviewer's governance feed must carry it.
    gov = temp_journal.governance_rows()
    assert any(g["setup"] == "write_integrity_degraded" for g in gov)


def test_integrity_events_dedupe_per_day(temp_journal):
    a = temp_journal.log_integrity_event("write_integrity_degraded",
                                         "positions.json: locked")
    b = temp_journal.log_integrity_event("write_integrity_degraded",
                                         "positions.json: locked again")
    assert a == b                                  # one row per target per day
    c = temp_journal.log_integrity_event("write_integrity_degraded",
                                         "bot_status.log: locked")
    assert c != a                                  # different target -> new row


def test_journalling_failure_never_breaks_the_write(tmp_path, monkeypatch):
    target = tmp_path / "positions.json"
    target.write_text("OLD")
    monkeypatch.setattr(os, "replace",
                        lambda s, d: (_ for _ in ()).throw(OSError(13, "locked")))
    monkeypatch.setattr(safe_io.time, "sleep", lambda s: None)

    def boom(*a, **k):
        raise RuntimeError("journal is down")
    monkeypatch.setattr(safe_io, "_journal_degradation", boom)

    with pytest.raises(RuntimeError):
        safe_io.atomic_write_text(str(target), "NEW")
    # The data still landed before journalling was attempted.
    assert target.read_text(encoding="utf-8") == "NEW"
