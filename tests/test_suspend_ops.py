"""
Ops fixes 2026-09-01: suspend detection, sleep suppression, local labels.

Autopsy: on 2026-08-31 the machine SLEPT at 12:36 ET (Kernel-Power 42) and
resumed at 21:06 ET. The worker was frozen, not hung — the watchdog was
frozen too, so nothing restarted, and the auto-shutdown fired the instant
the process resumed, five hours "late" only because wall-clock time had
moved on. A suspend and a hang need opposite remedies, so they must be
told apart.
"""

import sys

import pytest

import session_clock


# ------------------------------------------------------- suspend detection

def test_normal_cycle_is_not_a_suspend():
    assert session_clock.suspend_gap(1000.0, 1030.0, 30) is None
    assert session_clock.suspend_gap(1000.0, 1060.0, 30) is None   # 30s slow


def test_machine_sleep_is_detected():
    # The real gap: 12:36 ET -> 21:06 ET is 8.5 hours.
    base = 1_000_000.0
    gap = session_clock.suspend_gap(base, base + 8.5 * 3600, 30)
    assert gap is not None
    assert gap / 3600 == pytest.approx(8.5, abs=0.01)


def test_first_cycle_has_no_previous_reference():
    assert session_clock.suspend_gap(0, 99999.0, 30) is None


def test_threshold_boundary():
    base = 1_000_000.0
    assert session_clock.suspend_gap(base, base + 330.0, 30) is None   # exactly 300
    assert session_clock.suspend_gap(base, base + 331.0, 30) is not None


# ------------------------------------------------------- local time label

def test_local_str_uses_the_machine_timezone_not_hardcoded_nepal():
    from datetime import datetime
    import session_clock as sc
    out = sc.local_str({"session_end_et": "16:15"})
    # It must be a real clock time with a zone name, computed from the host.
    assert ":" in out
    hh, rest = out.split(":", 1)
    assert 0 <= int(hh) <= 23
    expected = (datetime.now(sc.EASTERN_TZ)
                .replace(hour=16, minute=15, second=0, microsecond=0)
                .astimezone().strftime("%H:%M"))
    assert out.startswith(expected)


def test_local_str_follows_the_configured_end_time():
    a = session_clock.local_str({"session_end_et": "16:15"})
    b = session_clock.local_str({"session_end_et": "12:00"})
    assert a != b                       # not a frozen string


def test_nepal_str_alias_still_works():
    cfg = {"session_end_et": "16:15"}
    assert session_clock.nepal_str(cfg) == session_clock.local_str(cfg)


# ------------------------------------------------------- sleep suppression

def test_keep_awake_is_safe_to_call_anywhere():
    """Windows suppresses sleep; elsewhere it must be a harmless no-op."""
    result = session_clock.keep_awake(True)
    assert isinstance(result, bool)
    session_clock.keep_awake(False)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only API")
def test_keep_awake_succeeds_on_windows():
    assert session_clock.keep_awake(True) is True
    session_clock.keep_awake(False)


def test_keep_awake_survives_a_broken_ctypes(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name == "ctypes":
            raise ImportError("no ctypes here")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", boom)
    assert session_clock.keep_awake(True) is False      # never raises
