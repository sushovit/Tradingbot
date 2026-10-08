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
    assert p["RunAtLoad"] is False          # installing never runs a job
    joined = " ".join(p["ProgramArguments"])
    for part in EXPECTED[name]:
        assert part in joined


@pytest.mark.parametrize("name", ["watchdog", "floor", "review", "outcomes",
                                  "intern"])
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
