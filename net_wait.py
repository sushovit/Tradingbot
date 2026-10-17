"""
net_wait.py — CEO 2026-10-17 (B). Don't start or relaunch the worker into
a network that cannot resolve the broker.

    venv/bin/python net_wait.py            # used by jobs/macos/start_if_open.sh

dns_ok() resolves the Alpaca hosts the worker needs. wait_for_network()
retries every 5 minutes until 09:30 ET, then every 15 minutes, until the
16:15 ET session end; every failed attempt is journaled as an ops event
(network_unavailable), so a morning lost to a DNS outage is on the record,
not just in a log. The watchdog uses dns_ok() to skip a pointless
kill-and-relaunch while the network is down (it runs every 15 minutes, so
it retries on its own).
"""

import socket
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HOSTS = ("paper-api.alpaca.markets", "data.alpaca.markets")
EARLY_RETRY_SECS = 5 * 60
LATE_RETRY_SECS = 15 * 60
SWITCH_AT = (9, 30)
GIVE_UP_AT = (16, 15)


def dns_ok(hosts=HOSTS, resolver=socket.getaddrinfo):
    """(ok, error). True only if every host resolves."""
    for host in hosts:
        try:
            resolver(host, 443)
        except (OSError, UnicodeError) as e:
            return False, f"{host}: {type(e).__name__}: {e}"
    return True, None


def journal_failure(who, attempt, now_et, error):
    """One ops row per failed attempt. log_integrity_event dedupes on the
    text before the first ':', so the attempt number leads."""
    try:
        import journal
        journal.init_db()
        journal.log_integrity_event(
            "network_unavailable",
            f"{who} attempt {attempt} at {now_et:%H%M} ET - {error}")
    except Exception as e:
        print(f"(could not journal network failure: {e})")


def retry_delay(now_et) -> int:
    return EARLY_RETRY_SECS if (now_et.hour, now_et.minute) < SWITCH_AT \
        else LATE_RETRY_SECS


def wait_for_network(who="start_worker", check=dns_ok, sleep=time.sleep,
                     now_fn=lambda: datetime.now(ET), record=journal_failure,
                     out=print) -> bool:
    """Block until the broker hosts resolve (True) or the session is over
    (False). Every failed attempt is recorded."""
    attempt = 0
    while True:
        ok, err = check()
        now = now_fn()
        if ok:
            if attempt:
                out(f"network back at {now:%H:%M} ET after {attempt} failed attempt(s)")
            return True
        attempt += 1
        record(who, attempt, now, err)
        if (now.hour, now.minute) >= GIVE_UP_AT:
            out(f"network still unavailable at {now:%H:%M} ET - giving up "
                f"for today ({attempt} attempts)")
            return False
        delay = retry_delay(now)
        out(f"network unavailable ({err}) - attempt {attempt}, retrying in "
            f"{delay // 60} min")
        sleep(delay)


if __name__ == "__main__":
    sys.exit(0 if wait_for_network() else 3)
