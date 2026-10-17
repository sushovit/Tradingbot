"""
CEO 2026-10-17 (B): DNS/network failure at start - retry every 5 min until
09:30 ET, then every 15 min, one ops event per failed attempt; the watchdog
does not relaunch into a dead network.
"""

import socket
from datetime import datetime
from zoneinfo import ZoneInfo

import net_wait

ET = ZoneInfo("America/New_York")


def clock(*hhmm):
    times = iter(datetime(2026, 10, 19, h, m, tzinfo=ET) for h, m in hhmm)
    return lambda: next(times)


def test_dns_ok_and_failure():
    assert net_wait.dns_ok(resolver=lambda h, p: [("ok",)]) == (True, None)

    def gaierror(host, port):
        raise socket.gaierror(8, "nodename nor servname provided")
    ok, err = net_wait.dns_ok(resolver=gaierror)
    assert ok is False and "paper-api.alpaca.markets" in err and "gaierror" in err


def test_retries_every_5_min_before_0930_then_every_15():
    results = iter([(False, "dns"), (False, "dns"), (False, "dns"), (True, None)])
    sleeps, recorded = [], []
    ok = net_wait.wait_for_network(
        check=lambda: next(results), sleep=sleeps.append,
        now_fn=clock((9, 25), (9, 29), (9, 30), (9, 45)),
        record=lambda who, n, now, err: recorded.append((who, n, f"{now:%H%M}")),
        out=lambda *a: None)
    assert ok is True
    assert sleeps == [300, 300, 900]
    assert recorded == [("start_worker", 1, "0925"), ("start_worker", 2, "0929"),
                        ("start_worker", 3, "0930")]


def test_gives_up_at_the_session_end():
    sleeps = []
    ok = net_wait.wait_for_network(
        check=lambda: (False, "dns"), sleep=sleeps.append,
        now_fn=clock((16, 0), (16, 15)), record=lambda *a: None,
        out=lambda *a: None)
    assert ok is False and sleeps == [900]


def test_every_failed_attempt_is_its_own_ops_row(temp_journal):
    for n, (h, m) in enumerate(((9, 25), (9, 30), (9, 45)), 1):
        net_wait.journal_failure("start_worker", n,
                                 datetime(2026, 10, 19, h, m, tzinfo=ET),
                                 "paper-api.alpaca.markets: gaierror")
    import sqlite3
    c = sqlite3.connect(temp_journal.DB_FILE)
    n = c.execute("SELECT COUNT(*) FROM decisions WHERE source='ops' AND "
                  "setup_name='network_unavailable'").fetchone()[0]
    c.close()
    assert n == 3


def test_the_start_script_waits_for_the_network_before_the_calendar():
    with open("jobs/macos/start_if_open.sh", encoding="utf-8") as f:
        src = f.read()
    assert src.index('"$PY" net_wait.py') < src.index("open_today = Broker().is_open_today()")


def test_the_watchdog_does_not_relaunch_into_a_dead_network(tmp_path, monkeypatch):
    import watchdog
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bot.run").write_text("999999")
    (tmp_path / "bot_status.log").write_text("[x] old")
    import os, time
    old = time.time() - 3600
    os.utime(tmp_path / "bot_status.log", (old, old))
    os.utime(tmp_path / "bot.run", (old, old))
    monkeypatch.setattr("session_clock.in_session_window", lambda cfg: True)
    monkeypatch.setattr(net_wait, "dns_ok", lambda *a, **k: (False, "dns down"))
    monkeypatch.setattr(net_wait, "journal_failure", lambda *a, **k: None)
    called = []
    monkeypatch.setattr(watchdog, "kill_stale_workers", lambda: called.append("kill") or 0)
    monkeypatch.setattr(watchdog, "relaunch_worker", lambda: called.append("launch") or True)
    monkeypatch.setattr("sys.argv", ["watchdog.py"])
    assert watchdog.main() == 0
    assert called == []
