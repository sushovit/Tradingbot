"""
alerts.py — S19b. The one way trading and order code talks to Discord.

    alerts.post_async(payload)

payload is a message string, an embed dict {"title", "color", "fields":
[(name, value), ...], "content"?}, or a CALLABLE returning either (or None
to send nothing). A callable is run on the background thread, so building
the message - a journal read, a config read - never happens on the caller's
path either.

Guarantees: a daemon thread per post; Discord timeout 10 s (net.webhook);
any failure is logged, redacted, and swallowed; post_async never raises
and callers never join it.

A short-lived process (orders.py, report.py) would lose a daemon thread at
exit, which is why the old code joined (`wait=True`, up to 30 s, inside the
order path). Instead an atexit hook drains pending posts for at most
DRAIN_S - after the process's own work is finished, never during it.
"""

import atexit
import logging
import os
import threading
import time

import net

logger = logging.getLogger(__name__)

TIMEOUT_S = net.DISCORD_TIMEOUT_S
DRAIN_S = 15

_pending = set()
_lock = threading.Lock()


def post_async(payload, url: str = None, sender=None):
    """Fire and forget. Returns the thread (tests may join it); never
    raises, never blocks."""
    try:
        t = threading.Thread(target=_run, args=(payload, url, sender),
                             name="discord-alert", daemon=True)
        with _lock:
            _pending.add(t)
        t.start()
        return t
    except Exception as e:                      # thread limit, shutdown...
        _log(f"Discord alert could not start: {e}")
        return None


def _run(payload, url, sender):
    try:
        if callable(payload):
            payload = payload()
        if payload is None:
            return
        (sender or _send)(payload, url)
    except Exception as e:
        title = payload.get("title") if isinstance(payload, dict) else None
        _log(f"Discord alert failed ({title or 'message'}): {e}")
    finally:
        with _lock:
            _pending.discard(threading.current_thread())


def _log(message: str):
    try:
        logger.warning(net.redact(message))
    except Exception:
        pass


def _send(payload, url: str = None):
    url = (url or os.getenv("DISCORD_WEBHOOK_URL", "")).strip()
    if not url:
        return None
    if isinstance(payload, str):
        wh = net.webhook(url, content=payload[:1900], timeout=TIMEOUT_S)
    else:
        from discord_webhook import DiscordEmbed
        wh = net.webhook(url, content=payload.get("content"),
                         timeout=TIMEOUT_S)
        embed = DiscordEmbed(title=payload["title"], color=payload["color"])
        for name, value in payload.get("fields", []):
            embed.add_embed_field(name=name, value=str(value))
        embed.set_timestamp()
        wh.add_embed(embed)
    resp = wh.execute()
    status = getattr(resp, "status_code", None)
    if isinstance(status, int) and status >= 400:
        raise RuntimeError(f"HTTP {status}")
    return resp


def pending() -> int:
    with _lock:
        return sum(1 for t in _pending if t.is_alive())


def drain(timeout: float = DRAIN_S) -> int:
    """Wait up to `timeout` for posts still in flight. Returns how many were
    still running when it gave up. Called at interpreter exit only."""
    deadline = time.monotonic() + timeout
    with _lock:
        threads = list(_pending)
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return pending()


atexit.register(drain)
