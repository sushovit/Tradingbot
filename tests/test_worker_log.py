"""
W1 (PM_PLAN.md): the worker writes to a rotating log, and a supervisor
restart leaves an auditable ops row. No network.

Why it matters: until now the worker's logger went to stderr only, and the
watchdog relaunches it with CREATE_NO_WINDOW — so every line from a
watchdog-started worker was discarded. Crash restarts left no trace at all
outside a console nobody was watching.
"""

import logging
import logging.handlers
import os

import pytest

import run_worker

WINDOWS_ONLY = pytest.mark.skipif(
    __import__("sys").platform != "win32",
    reason="Windows worker supervisor")


@pytest.fixture
def clean_root():
    """Swap the root logger's handlers out for the duration of a test."""
    root = logging.getLogger()
    saved, saved_level = root.handlers[:], root.level
    root.handlers = []
    yield root
    for h in root.handlers:
        try:
            h.close()
        except Exception:
            pass
    root.handlers, root.level = saved, saved_level


# ============================================================ the handler

def test_rotating_file_handler_is_attached_to_root(clean_root, tmp_path):
    """Root, not a named logger: the supervisor and the loop both log through
    streamlit_app's module logger, which propagates to root."""
    target = str(tmp_path / "worker.log")
    run_worker.setup_logging(target)

    files = [h for h in clean_root.handlers
             if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(files) == 1
    handler = files[0]
    assert handler.maxBytes == 5 * 1024 * 1024
    assert handler.backupCount == 5
    assert (handler.encoding or "").lower().replace("-", "") == "utf8"


def test_console_output_survives_the_file_handler(clean_root, tmp_path):
    """streamlit_app calls basicConfig() at import, and basicConfig is a
    NO-OP once root has handlers. Attaching only the file handler would take
    the console away silently — including the 'SPY regime refreshed:' line
    the desk reads on the first cycle."""
    run_worker.setup_logging(str(tmp_path / "worker.log"))
    # pytest attaches its own LogCaptureHandler (also a StreamHandler) to
    # root, so identify OURS by its formatter rather than by type alone.
    consoles = [h for h in clean_root.handlers
                if getattr(h, "_worker_handler", None) == "console"]
    assert len(consoles) == 1
    assert not isinstance(consoles[0], logging.FileHandler)

    # And basicConfig really is inert now, which is why the above matters.
    before = len(clean_root.handlers)
    logging.basicConfig(level=logging.INFO)
    assert len(clean_root.handlers) == before


def test_setup_is_idempotent(clean_root, tmp_path):
    """The watchdog can relaunch; a second call must not duplicate every
    line in the file."""
    target = str(tmp_path / "worker.log")
    run_worker.setup_logging(target)
    run_worker.setup_logging(target)
    for kind in ("file", "console"):
        assert sum(1 for h in clean_root.handlers
                   if getattr(h, "_worker_handler", None) == kind) == 1


def test_lines_actually_reach_the_file_in_utf8(clean_root, tmp_path):
    """The emoji that crashed the watchdog under cp1252 must write cleanly."""
    target = str(tmp_path / "worker.log")
    run_worker.setup_logging(target)
    logging.getLogger("streamlit_app").info("\U0001f501 worker start PID 123")
    for h in clean_root.handlers:
        h.flush()
    body = open(target, encoding="utf-8").read()
    assert "worker start PID 123" in body
    assert "\U0001f501" in body


def test_a_foreign_stream_handler_does_not_suppress_ours(clean_root,
                                                          tmp_path):
    """Root may already carry somebody else's StreamHandler. Type-sniffing
    would read that as 'console already covered' and skip ours."""
    foreign = logging.StreamHandler()
    clean_root.addHandler(foreign)
    run_worker.setup_logging(str(tmp_path / "worker.log"))
    assert sum(1 for h in clean_root.handlers
               if getattr(h, "_worker_handler", None) == "console") == 1


def test_log_lands_in_logs_dir_by_default():
    assert run_worker.WORKER_LOG.replace("\\", "/") == "logs/worker.log"


# ============================================================ the start line

def test_worker_loop_logs_its_pid_and_reset(monkeypatch, tmp_path, caplog):
    """One line per process start, so a silent watchdog relaunch is visible
    in the file afterwards."""
    import streamlit_app as app
    monkeypatch.chdir(tmp_path)

    class Boom(Exception):
        pass

    def explode():
        raise Boom("no broker in tests")

    monkeypatch.setattr(app, "Broker", explode)
    monkeypatch.setattr(app, "write_status", lambda *a, **k: None)
    with caplog.at_level(logging.INFO, logger="streamlit_app"):
        app._worker_loop()
    text = caplog.text
    assert "worker start PID" in text
    assert str(os.getpid()) in text
    assert "cycle counter reset" in text


# ============================================================ restart event

@WINDOWS_ONLY
def test_supervisor_journals_a_restart_event(monkeypatch, tmp_path,
                                             temp_journal):
    import streamlit_app as app
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot.run").write_text("1")
    monkeypatch.setattr(app, "journal", temp_journal)
    monkeypatch.setattr(app, "write_status", lambda *a, **k: None)
    monkeypatch.setattr(app, "send_discord_notification", lambda *a, **k: None)
    monkeypatch.setattr(app.a_time, "sleep", lambda s: None)

    calls = {"n": 0}

    def crash_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("simulated data explosion")
        (tmp_path / "bot.run").unlink()

    monkeypatch.setattr(app, "_worker_loop", crash_once)
    app.live_bot_worker()

    rows = temp_journal.governance_rows()
    ops = [r for r in rows if r["setup"] == "worker_restarted"]
    assert len(ops) == 1, rows
    assert ops[0]["source"] == "ops"
    # The exception type leads the details, which is also the dedupe key.
    assert "ValueError" in ops[0]["ticker"]
    assert "loop crash #1" in ops[0]["note"]


@WINDOWS_ONLY
def test_restart_event_survives_a_failing_write_status(monkeypatch, tmp_path,
                                                      temp_journal):
    """A dead Discord webhook or a failing status write must not be able to
    swallow the audit record of a crash."""
    import streamlit_app as app
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot.run").write_text("1")
    monkeypatch.setattr(app, "journal", temp_journal)
    monkeypatch.setattr(app.a_time, "sleep", lambda s: None)

    def bad_status(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(app, "write_status", bad_status)
    monkeypatch.setattr(app, "send_discord_notification", lambda *a, **k: None)

    calls = {"n": 0}

    def crash_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        (tmp_path / "bot.run").unlink()

    monkeypatch.setattr(app, "_worker_loop", crash_once)
    app.live_bot_worker()

    ops = [r for r in temp_journal.governance_rows()
           if r["setup"] == "worker_restarted"]
    assert len(ops) == 1


@WINDOWS_ONLY
def test_repeated_identical_crashes_log_one_row_per_day(monkeypatch, tmp_path,
                                                        temp_journal):
    """log_integrity_event dedupes on the leading token of `details`. Leading
    with the exception type means a crash LOOP reads as one fault, not fifty
    — while a genuinely different failure still gets its own row."""
    import streamlit_app as app
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot.run").write_text("1")
    monkeypatch.setattr(app, "journal", temp_journal)
    monkeypatch.setattr(app, "write_status", lambda *a, **k: None)
    monkeypatch.setattr(app, "send_discord_notification", lambda *a, **k: None)
    monkeypatch.setattr(app.a_time, "sleep", lambda s: None)

    calls = {"n": 0}

    def crash_thrice():
        calls["n"] += 1
        if calls["n"] <= 3:
            raise ValueError("same fault every time")
        (tmp_path / "bot.run").unlink()

    monkeypatch.setattr(app, "_worker_loop", crash_thrice)
    app.live_bot_worker()

    ops = [r for r in temp_journal.governance_rows()
           if r["setup"] == "worker_restarted"]
    assert len(ops) == 1                    # three crashes, one fault


@WINDOWS_ONLY
def test_a_clean_exit_journals_nothing(monkeypatch, tmp_path, temp_journal):
    import streamlit_app as app
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot.run").write_text("1")
    monkeypatch.setattr(app, "journal", temp_journal)
    monkeypatch.setattr(app, "_worker_loop", lambda: None)
    app.live_bot_worker()
    assert [r for r in temp_journal.governance_rows()
            if r["setup"] == "worker_restarted"] == []
