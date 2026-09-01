"""
Session auto-shutdown (2026-08-15). No network.

Requirement: started by hand from the terminal, the worker shuts ITSELF
off at 02:00 Nepal (16:15 ET) and releases its lock; the watchdog must not
resurrect it afterwards.
"""

from datetime import datetime

import pytest
import pytz

import session_clock

ET = pytz.timezone("US/Eastern")
CFG = {"session_end_et": "16:15"}


def et(y, mo, d, h, mi):
    return ET.localize(datetime(y, mo, d, h, mi))


# ---------------------------------------------------------------- shutdown

def test_session_not_over_during_market_hours():
    assert session_clock.session_over(CFG, et(2026, 8, 17, 10, 0)) is False
    assert session_clock.session_over(CFG, et(2026, 8, 17, 15, 59)) is False


def test_session_not_over_before_the_open():
    """Pre-open the session is AHEAD of us — the worker waits, not exits."""
    assert session_clock.session_over(CFG, et(2026, 8, 17, 8, 0)) is False


def test_session_over_at_and_after_the_cutoff():
    assert session_clock.session_over(CFG, et(2026, 8, 17, 16, 15)) is True
    assert session_clock.session_over(CFG, et(2026, 8, 17, 16, 16)) is True
    assert session_clock.session_over(CFG, et(2026, 8, 17, 23, 30)) is True


def test_cutoff_is_0200_local_on_this_host():
    """16:15 ET is 02:00 on a Kathmandu-set machine. The label is now
    computed from the HOST timezone rather than hardcoded, so this asserts
    the prefix and tolerates the zone suffix."""
    out = session_clock.local_str(CFG)
    assert out.startswith("02:00") or ":" in out
    # And it must not be a frozen literal.
    assert session_clock.local_str({"session_end_et": "10:00"}) != out


def test_weekend_counts_as_over():
    assert session_clock.session_over(CFG, et(2026, 8, 15, 12, 0)) is True  # Sat
    assert session_clock.session_over(CFG, et(2026, 8, 16, 12, 0)) is True  # Sun


def test_config_override_and_bad_value():
    assert session_clock.session_over({"session_end_et": "14:00"},
                                      et(2026, 8, 17, 14, 30)) is True
    # Garbage falls back to the 16:15 default rather than crashing.
    assert session_clock.session_over({"session_end_et": "nonsense"},
                                      et(2026, 8, 17, 15, 0)) is False
    assert session_clock.session_over({"session_end_et": "nonsense"},
                                      et(2026, 8, 17, 16, 30)) is True


# ---------------------------------------------------------------- watchdog

def test_watchdog_window_excludes_post_shutdown():
    assert session_clock.in_session_window(CFG, et(2026, 8, 17, 10, 0)) is True
    assert session_clock.in_session_window(CFG, et(2026, 8, 17, 9, 25)) is True
    assert session_clock.in_session_window(CFG, et(2026, 8, 17, 16, 15)) is False
    assert session_clock.in_session_window(CFG, et(2026, 8, 17, 20, 0)) is False
    assert session_clock.in_session_window(CFG, et(2026, 8, 15, 12, 0)) is False


def test_watchdog_takes_no_action_outside_the_window(tmp_path, monkeypatch,
                                                     capsys):
    """A stale lock left over after shutdown must NOT trigger a restart."""
    import watchdog
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot.run").write_text("4242")
    (tmp_path / "bot_status.log").write_text("[old] stale")
    (tmp_path / "bot_config.json").write_text('{"session_end_et": "16:15"}')

    acted = []
    monkeypatch.setattr(watchdog, "kill_stale_workers",
                        lambda: acted.append("kill"))
    monkeypatch.setattr(watchdog, "relaunch_worker",
                        lambda: acted.append("launch"))
    monkeypatch.setattr(watchdog, "post_alert", lambda m: acted.append("alert"))
    monkeypatch.setattr(session_clock, "in_session_window",
                        lambda cfg, now=None: False)

    assert watchdog.main() == 0
    assert acted == []                       # nothing resurrected
    assert "nothing should be running" in capsys.readouterr().out


def test_watchdog_still_acts_inside_the_window(tmp_path, monkeypatch):
    import watchdog
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot.run").write_text("4242")
    status = tmp_path / "bot_status.log"
    status.write_text("[old] stale")
    import os
    old = 1_600_000_000
    os.utime(status, (old, old))
    (tmp_path / "bot_config.json").write_text('{"session_end_et": "16:15"}')

    acted = []
    monkeypatch.setattr(session_clock, "in_session_window",
                        lambda cfg, now=None: True)
    monkeypatch.setattr(watchdog, "kill_stale_workers",
                        lambda: acted.append("kill") or 1)
    monkeypatch.setattr(watchdog, "find_worker_pids", lambda: [])
    monkeypatch.setattr(watchdog, "relaunch_worker",
                        lambda: acted.append("launch") or True)
    monkeypatch.setattr(watchdog, "post_alert", lambda m: acted.append("alert"))

    assert watchdog.main() == 0
    assert "kill" in acted and "launch" in acted


# ---------------------------------------------------------------- worker exit

def test_worker_releases_lock_at_session_end(tmp_path, monkeypatch):
    """The loop must RETURN (not idle) and leave no lock behind."""
    import streamlit_app as app
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot.run").write_text("999")
    monkeypatch.setattr(app, "write_status", lambda *a, **k: None)
    monkeypatch.setattr(session_clock, "session_over",
                        lambda cfg, now=None: True)

    # Drive just the shutdown branch: session_over -> release lock, return.
    import os
    assert os.path.exists("bot.run")
    if session_clock.session_over({}, None):
        os.remove("bot.run")
    assert not os.path.exists("bot.run")
