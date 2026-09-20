"""
S1 (PM_PLAN.md / agenda 10.3): a second worker cannot start while the owner
is alive, and a gatekeeper rejection survives a restart. No network.

THE INCIDENT, 2026-09-17. SPCX was asked at 09:54 and declined at conviction
68. A second worker was started by hand at 09:57; the in-memory rejection
cache died with the first process, the same signal bar was re-asked, came
back 78, and was bought. Two independent failures let that happen:

  (a) the start guard keyed on HEARTBEAT FRESHNESS only, so a live worker
      with a stale heartbeat did not block a second start, and
  (b) the rejection cache was a set inside _worker_loop.

A rejection that does not survive a restart is not a rejection, it is a
delay.
"""

import sqlite3

import pytest

import run_worker


# ============================================ (a) second start refused

def test_second_start_refused_while_the_owner_pid_is_alive(
        tmp_path, monkeypatch, capsys):
    """The core of the fix: a LIVE owner blocks, regardless of how old the
    heartbeat is."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot.run").write_text("4242")
    # Deliberately NO heartbeat file at all — the old guard would wave this
    # through, because it had nothing to measure.
    monkeypatch.setattr(run_worker, "pid_alive", lambda pid: pid == 4242)

    claimed = []
    monkeypatch.setattr(run_worker, "write_lock",
                        lambda *a, **k: claimed.append(a))

    rc = run_worker.main([])
    out = capsys.readouterr().out

    assert rc == 1
    assert claimed == []                      # never took the lock
    assert "refusing to start" in out
    assert "4242" in out                      # names the owner
    assert "ALIVE" in out


def test_a_stale_heartbeat_no_longer_lets_a_second_worker_in(
        tmp_path, monkeypatch, capsys):
    """The exact 09-17 shape: owner alive, heartbeat long past the 60s
    window. Before S1 this started a second instance."""
    import os
    import time
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot.run").write_text("777")
    status = tmp_path / "bot_status.log"
    status.write_text("[old] Cycle #1")
    old = time.time() - 3600                  # an hour stale
    os.utime(status, (old, old))
    monkeypatch.setattr(run_worker, "pid_alive", lambda pid: pid == 777)
    monkeypatch.setattr(run_worker, "write_lock", lambda *a, **k: None)

    assert run_worker.another_worker_is_alive() is False   # heartbeat says ok
    assert run_worker.running_owner_pid() == 777           # the PID says no
    assert run_worker.main([]) == 1


def test_a_dead_owner_does_not_block_a_start(tmp_path, monkeypatch):
    """A crashed worker leaves its lock behind; that must not wedge the desk
    shut."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot.run").write_text("999")
    monkeypatch.setattr(run_worker, "pid_alive", lambda pid: False)
    assert run_worker.running_owner_pid() is None

    claimed = []
    monkeypatch.setattr(run_worker, "write_lock",
                        lambda *a, **k: claimed.append(a))

    class Boom(Exception):
        pass

    real_import = __import__

    def fake_import(name, *a, **k):
        if name == "streamlit_app":
            raise Boom()
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", fake_import)
    with pytest.raises(Boom):
        run_worker.main([])
    assert claimed                            # it DID claim the lock


def test_no_lock_file_means_no_owner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run_worker.running_owner_pid() is None


def test_force_takeover_still_works_against_a_live_owner(
        tmp_path, monkeypatch, capsys):
    """The override must survive the tightening, or the watchdog's relaunch
    path breaks."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot.run").write_text("555")
    monkeypatch.setattr(run_worker, "pid_alive", lambda pid: pid == 555)

    killed, claimed = [], []
    monkeypatch.setattr(run_worker, "kill_pid",
                        lambda pid, **k: killed.append(pid) or True)
    monkeypatch.setattr(run_worker, "write_lock",
                        lambda *a, **k: claimed.append(a))

    class Boom(Exception):
        pass

    real_import = __import__

    def fake_import(name, *a, **k):
        if name == "streamlit_app":
            raise Boom()
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", fake_import)
    with pytest.raises(Boom):
        run_worker.main(["--force-takeover"])

    assert killed == [555]
    assert claimed
    assert "alive" in capsys.readouterr().out


def test_force_takeover_still_aborts_if_the_owner_survives(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot.run").write_text("888")
    monkeypatch.setattr(run_worker, "pid_alive", lambda pid: True)
    monkeypatch.setattr(run_worker, "kill_pid", lambda pid, **k: False)
    claimed = []
    monkeypatch.setattr(run_worker, "write_lock",
                        lambda *a, **k: claimed.append(a))

    assert run_worker.main(["--force-takeover"]) == 2
    assert claimed == []
    assert "could not kill owner" in capsys.readouterr().out


# ============================================ (b) rejection survives restart

def test_a_rejection_survives_a_simulated_restart(temp_journal):
    """The whole point: the in-memory set is gone, the journal is not."""
    bar = "2026-09-17"
    assert temp_journal.is_gatekeeper_rejected("SPCX", "mean_reversion_reclaim",
                                               bar) is False

    # 09:54 — the gatekeeper declines at conviction 68.
    assert temp_journal.cache_gatekeeper_rejection(
        "SPCX", "mean_reversion_reclaim", bar) is True

    # 09:57 — a hand restart. A fresh process has an EMPTY set.
    gatekeeper_rejected = set()
    gate_key = ("SPCX", "mean_reversion_reclaim", bar)
    blocked = gate_key in gatekeeper_rejected or \
        temp_journal.is_gatekeeper_rejected("SPCX", "mean_reversion_reclaim",
                                            bar)
    assert blocked is True, "the re-ask that bought SPCX at 78"


def test_caching_the_same_bar_twice_is_a_no_op(temp_journal):
    bar = "2026-09-17"
    assert temp_journal.cache_gatekeeper_rejection("SPCX", "s", bar) is True
    assert temp_journal.cache_gatekeeper_rejection("SPCX", "s", bar) is False
    conn = sqlite3.connect(temp_journal.DB_FILE)
    n = conn.execute("SELECT COUNT(*) FROM gatekeeper_cache").fetchone()[0]
    conn.close()
    assert n == 1


def test_the_cache_is_per_ticker_setup_and_bar(temp_journal):
    temp_journal.cache_gatekeeper_rejection("SPCX", "reclaim", "2026-09-17")
    for args in (("ARM", "reclaim", "2026-09-17"),
                 ("SPCX", "momentum", "2026-09-17"),
                 ("SPCX", "reclaim", "2026-09-16")):
        assert temp_journal.is_gatekeeper_rejected(*args) is False


def test_the_cache_is_scoped_to_today(temp_journal):
    """'check the journal for that key on today's date' — yesterday's
    rejection must not block a fresh bar judged today."""
    temp_journal.cache_gatekeeper_rejection("SPCX", "reclaim", "2026-09-17",
                                            date_str="2026-09-16")
    assert temp_journal.is_gatekeeper_rejected(
        "SPCX", "reclaim", "2026-09-17", date_str="2026-09-17") is False
    assert temp_journal.is_gatekeeper_rejected(
        "SPCX", "reclaim", "2026-09-17", date_str="2026-09-16") is True


def test_a_missing_bar_key_is_never_cached(temp_journal):
    """No bar, no key — caching on None would block every later signal for
    that ticker."""
    assert temp_journal.cache_gatekeeper_rejection("SPCX", "reclaim", None) \
        is False
    assert temp_journal.is_gatekeeper_rejected("SPCX", "reclaim", None) is False


def test_the_cache_lives_outside_the_decisions_table(temp_journal):
    """Writing these into `decisions` would put cache entries into
    decision_counts, governance_rows and the training export."""
    before = temp_journal.decision_counts()["total"]
    temp_journal.cache_gatekeeper_rejection("SPCX", "reclaim", "2026-09-17")
    assert temp_journal.decision_counts()["total"] == before
    assert [r for r in temp_journal.governance_rows()
            if r["ticker"] == "SPCX"] == []


def test_the_worker_consults_and_writes_the_journal():
    """Source assertion: both sites must go through the journal, not only
    the set."""
    with open("streamlit_app.py", encoding="utf-8") as f:
        body = f.read()
    read_site = body[body.index("gate_key = (ticker, signal.setup_name"):]
    assert "journal.is_gatekeeper_rejected(" in read_site[:600]
    assert "journal.cache_gatekeeper_rejection(" in body


def test_errors_are_not_cached():
    """Errors are transient and must be retried; only a genuine rejection is
    persisted. The write sits under the `if \"error\" not in verdict` branch."""
    with open("streamlit_app.py", encoding="utf-8") as f:
        body = f.read()
    idx = body.index("journal.cache_gatekeeper_rejection(")
    preceding = body[max(0, idx - 900):idx]
    assert 'if "error" not in verdict:' in preceding
