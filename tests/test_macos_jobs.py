"""
S12 — launchd jobs for the macOS host (jobs/macos). No job is installed or
run here: the plists are parsed, and the start script is exercised against
a FAKE venv python so no broker call and no worker launch can happen.
"""

import os
import plistlib
import shutil
import subprocess
import sys

import pytest

JOBS = os.path.join("jobs", "macos")
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="bash")

EXPECTED = {
    "start_worker": ["start_if_open.sh"],
    "watchdog": ["watchdog.py"],
    "floor": ["floor.py", "--to-file", "--discord"],
    "review": ["review_bot.py"],
    "outcomes": ["outcomes.py"],
    "intern": ["intern_desk.py", "--trade"],
    "snapshot": ["snapshot.py", "drop.py --discord"],
    "shadow_warm": ["local_analyst.py", "--warm"],      # S28: 09:20 ET
    "missed_movers": ["missed_movers.py"],              # S33: 16:20 ET
    "keepawake": ["caffeinate", "-i", "-t 26700"],      # CEO 10-20: 09:05 ET
    "catalysts": ["catalysts.py"],                      # S36: 09:00 ET scan
    "catalysts_post": ["catalysts.py", "--post"],       # S36: 09:20 ET post
    "awake_week": ["jobs/awake_week.py"],               # CEO 10-21: Sun 20:00
    "scoreboard": ["scoreboard.py"],                    # CEO 10-21: Fri 16:30
    "watch_movers": ["watch_movers.py"],                # CEO 10-23: every 30 min
    "credit_report": ["credit.py"],                     # CEO 10-24: 16:20 ET
    "bars_5m": ["bars_store.py", "--append"],          # CEO 10-25: 17:00 ET
    "shadow_unload": ["local_analyst.py", "--unload"],  # CEO 10-25: 16:30 ET
    "cleanup": ["disk_cleanup.py", "--apply"],          # CEO 10-25: Sat 12:00 ET
}


def load(name):
    with open(os.path.join(JOBS, f"com.tradingbot.{name}.plist"), "rb") as f:
        return plistlib.load(f)


def weekday_times(plist):
    # A daily slot has no Weekday; sort it after the weekday slots.
    return sorted(((s.get("Weekday"), s["Hour"], s["Minute"])
                   for s in plist["StartCalendarInterval"]),
                  key=lambda t: (t[0] is None, t[0] or 0, t[1], t[2]))


def test_one_plist_per_job_and_nothing_else():
    found = sorted(n for n in os.listdir(JOBS) if n.endswith(".plist"))
    assert found == sorted(f"com.tradingbot.{n}.plist" for n in EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_job_has_the_shared_environment(name):
    p = load(name)
    assert p["Label"] == f"com.tradingbot.{name}"
    assert p["EnvironmentVariables"] == {"PYTHONUTF8": "1",
                                         "TZ": "America/New_York"}
    assert p["WorkingDirectory"] == "__REPO__"
    assert p["StandardOutPath"] == f"__REPO__/logs/{name}.log"
    assert p["StandardErrorPath"] == f"__REPO__/logs/{name}.log"
    # installing never runs a job - except the always-awake hold, which must
    # restart itself after a reboot (CEO 2026-10-21) and only runs caffeinate
    assert p["RunAtLoad"] is (name == "awake_week")
    joined = " ".join(p["ProgramArguments"])
    for part in EXPECTED[name]:
        assert part in joined


@pytest.mark.parametrize("name", ["watchdog", "floor", "review", "outcomes",
                                  "intern", "missed_movers"])
def test_python_jobs_run_through_the_venv_wrapper(name):
    args = load(name)["ProgramArguments"]
    assert args[:2] == ["/bin/bash", "__REPO__/jobs/macos/run_job.sh"]


def test_run_job_uses_the_venv_python():
    with open(os.path.join(JOBS, "run_job.sh"), encoding="utf-8") as f:
        assert "exec venv/bin/python" in f.read()


def test_schedules_match_the_work_order():
    weekdays = range(1, 6)
    assert weekday_times(load("start_worker")) == [(d, 9, 25) for d in weekdays]
    # Hourly floor posts through the session, as the laptop ran them
    # (20:15-01:15 Nepal), plus the post-close report.
    floor = [(10, 30), (11, 30), (12, 30), (13, 30), (14, 30), (15, 30), (16, 16)]
    assert weekday_times(load("floor")) == [(d, h, m) for d in weekdays
                                            for h, m in floor]
    assert weekday_times(load("review")) == [(d, 16, 30) for d in weekdays]
    assert weekday_times(load("outcomes")) == [(d, 16, 45) for d in weekdays]
    assert weekday_times(load("missed_movers")) == [(d, 16, 20) for d in weekdays]
    assert weekday_times(load("intern")) == [(d, 8, 0) for d in weekdays]
    # Intraday snapshots at 10:00 and 15:00 (the laptop's 19:45 / 00:45
    # Nepal), Mon-Fri, plus the daily 18:00.
    assert weekday_times(load("snapshot")) == (
        [(d, h, 0) for d in weekdays for h in (10, 15)] + [(None, 18, 0)])


def test_the_watchdog_runs_every_quarter_hour_from_nine_to_five():
    times = weekday_times(load("watchdog"))
    for d in range(1, 6):
        mine = [(h, m) for wd, h, m in times if wd == d]
        assert mine[0] == (9, 0) and mine[-1] == (17, 0)
        assert len(mine) == 33                     # 8 h x 4 + 17:00
        assert all(m in (0, 15, 30, 45) for _, m in mine)
    assert {wd for wd, _, _ in times} == {1, 2, 3, 4, 5}


def test_jobs_that_launch_the_worker_abandon_its_process_group():
    for name in ("start_worker", "watchdog"):
        assert load(name).get("AbandonProcessGroup") is True


@POSIX_ONLY
def test_plutil_accepts_every_plist():
    if not shutil.which("plutil"):
        pytest.skip("plutil is macOS-only")
    for name in EXPECTED:
        path = os.path.join(JOBS, f"com.tradingbot.{name}.plist")
        r = subprocess.run(["plutil", "-lint", path], capture_output=True,
                           text=True)
        assert r.returncode == 0, r.stdout


# ================================================== install / uninstall

def _src(name):
    with open(os.path.join(JOBS, name), encoding="utf-8") as f:
        return f.read()


def test_install_checks_the_timezone_before_anything_else():
    src = _src("install.sh")
    tz = src.index("readlink /etc/localtime")
    assert "*America/New_York)" in src
    assert tz < src.index("launchctl bootstrap")
    assert tz < src.index("mkdir -p")
    assert "plutil -lint" in src and "launchctl print" in src


def test_neither_script_starts_anything():
    for name in ("install.sh", "uninstall.sh"):
        src = _src(name)
        for verb in ("kickstart", "launchctl start", "run_worker",
                     "start_if_open"):
            assert verb not in src, f"{name} must not {verb}"


def test_uninstall_boots_out_every_tradingbot_job():
    src = _src("uninstall.sh")
    assert "com\\.tradingbot\\." in src
    assert 'launchctl bootout "$DOMAIN/$label"' in src


@POSIX_ONLY
def test_install_aborts_on_a_non_new_york_mac():
    """Run for real only where it is guaranteed to abort before touching
    launchd: on a Mac whose timezone is NOT New York."""
    if not os.path.islink("/etc/localtime"):
        pytest.skip("no /etc/localtime link")
    if os.readlink("/etc/localtime").endswith("America/New_York"):
        pytest.skip("this Mac is on New York time; install would proceed")
    r = subprocess.run(["bash", os.path.join(JOBS, "install.sh")],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 1
    assert "not America/New_York" in r.stdout


# ================================================== start_if_open.sh

FAKE_PYTHON = """#!/bin/bash
# Fake venv python: answers the calendar check with $GATE_RC and records a
# spawn instead of launching anything.
case "$*" in
    *net_wait.py*)   [ -n "$NET_RC" ] && { echo "NETWAIT"; exit "$NET_RC"; }; exit 0 ;;
    *is_open_today*) [ -n "$GATE_MSG" ] && echo "$GATE_MSG"; exit "$GATE_RC" ;;
    *spawn_worker*)  echo "SPAWNED"; exit 0 ;;
esac
exit 99
"""


@pytest.fixture
def fake_repo(tmp_path):
    jobs = tmp_path / "jobs" / "macos"
    jobs.mkdir(parents=True)
    shutil.copy(os.path.join(JOBS, "start_if_open.sh"), jobs)
    py = tmp_path / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text(FAKE_PYTHON)
    py.chmod(0o755)
    return tmp_path


def start(fake_repo, rc, msg=""):
    env = dict(os.environ, GATE_RC=str(rc), GATE_MSG=msg)
    r = subprocess.run(["bash", str(fake_repo / "jobs" / "macos"
                                    / "start_if_open.sh")],
                       capture_output=True, text=True, env=env, timeout=30)
    return r.returncode, r.stdout


@POSIX_ONLY
def test_start_on_a_trading_day(fake_repo):
    rc, out = start(fake_repo, 0)
    assert "trading day - starting worker" in out
    assert "SPAWNED" in out and rc == 0


@POSIX_ONLY
def test_no_start_on_a_closed_day(fake_repo):
    rc, out = start(fake_repo, 1)
    assert "not a trading day - worker not started" in out
    assert "SPAWNED" not in out and rc == 0


@POSIX_ONLY
def test_calendar_error_fails_open(fake_repo):
    rc, out = start(fake_repo, 2, "calendar check failed (RuntimeError: x)")
    assert "calendar check failed" in out
    assert "starting worker anyway (fail-open)" in out
    assert "SPAWNED" in out


def test_the_gate_maps_an_exception_to_exit_two():
    """The fail-open branch is only reachable if the real gate exits 2 on an
    exception instead of Python's default 1 (= 'closed')."""
    src = _src("start_if_open.sh")
    gate = src[src.index("\"$PY\" -c '"):src.index("rc=$?")]
    assert "except Exception" in gate and "sys.exit(2)" in gate
    assert "is_open_today()" in gate



@POSIX_ONLY
def test_no_network_until_session_end_means_no_start(fake_repo):
    import os, subprocess
    env = dict(os.environ, GATE_RC="0", GATE_MSG="", NET_RC="3")
    r = subprocess.run(["bash", str(fake_repo / "jobs" / "macos" / "start_if_open.sh")],
                       capture_output=True, text=True, env=env, timeout=30)
    assert "network unavailable until the session end - worker not started" in r.stdout
    assert "SPAWNED" not in r.stdout and r.returncode == 0


def test_keepawake_holds_the_mac_awake_0905_to_1630_on_weekdays():
    p = load("keepawake")
    assert p["ProgramArguments"] == ["/usr/bin/caffeinate", "-i", "-t", "26700"]
    assert weekday_times(p) == [(d, 9, 5) for d in range(1, 6)]
    assert 9 * 3600 + 5 * 60 + 26700 == 16 * 3600 + 30 * 60   # until 16:30 ET


def test_run_worker_calls_keep_awake_first(monkeypatch):
    import run_worker
    import session_clock
    calls = []
    monkeypatch.setattr(session_clock, "keep_awake", lambda on=True: calls.append(on) or True)
    monkeypatch.setattr(run_worker, "live_heartbeat_age", lambda: calls.append("lock") or None)
    monkeypatch.setattr(run_worker, "running_owner_pid", lambda: 1)
    monkeypatch.setattr(run_worker, "another_worker_is_alive", lambda: True)
    monkeypatch.setattr(run_worker, "read_lock_pid", lambda: 1)
    assert run_worker.main([]) == 1                     # refused: another worker
    assert calls[:2] == [True, "lock"]


def test_awake_week_holds_sun_2000_to_fri_1630_and_survives_a_reboot():
    import importlib.util
    from datetime import datetime
    from zoneinfo import ZoneInfo
    p = load("awake_week")
    assert p["RunAtLoad"] is True                       # a reboot restarts it
    assert p["KeepAlive"] == {"SuccessfulExit": False}  # restart only if killed
    assert weekday_times(p) == [(0, 20, 0)]             # Sunday 20:00 ET
    assert p["ProgramArguments"][-1].endswith("jobs/awake_week.py")
    assert "run_job.sh" not in " ".join(p["ProgramArguments"])   # no wall-clock kill
    spec = importlib.util.spec_from_file_location("awake_week", os.path.join("jobs", "awake_week.py"))
    a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
    et = ZoneInfo("America/New_York")
    at = lambda s: datetime.fromisoformat(s).replace(tzinfo=et)
    assert a.seconds_left(at("2026-10-25 20:00")) == 419400      # Sun 20:00 -> Fri 16:30
    assert a.seconds_left(at("2026-10-21 05:50")) == 211200      # mid-week reboot
    assert a.seconds_left(at("2026-10-23 16:30")) == 0           # Fri close: done
    assert a.seconds_left(at("2026-10-24 12:00")) == 0           # Saturday
    assert a.seconds_left(at("2026-10-25 19:59")) == 0           # before Sun 20:00
    # the window across the 2026-11-01 DST change is still exact wall-clock
    assert a.seconds_left(at("2026-11-01 20:00")) == 419400
