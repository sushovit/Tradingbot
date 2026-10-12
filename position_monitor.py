"""
position_monitor.py — S18. A light loop that ONLY manages open positions.

The worker's main cycle scans the whole universe on 5-minute bars and asks
the gatekeeper about signals, so a full cycle takes a minute or more. Exits
and the profit lock should not wait for that. This loop runs every
MONITOR_INTERVAL_SECS during 09:30-16:00 ET and, for each open BOT position:
detects a filled exit and applies the profit lock / intraday trail - through
the same functions the main cycle uses (streamlit_app passes them in as
`manage`). Scanning and entries stay on the main cycle.

Safety:
- One lock (streamlit_app.POSITIONS_LOCK) is held for each position's
  management, here and in the main cycle, and the position is re-read under
  it, so an exit is never handled twice and a stop never raised twice.
- Stops only move up: maybe_lock_profit / maybe_ratchet_stop enforce it.
- Rate limits: a 429 from Alpaca backs the loop off (60 s, doubling to 10
  min); any success resets it.
- Positions are written after every change by the functions it calls.
"""

import logging
import threading
import time
from datetime import time as dtime

logger = logging.getLogger(__name__)

MONITOR_INTERVAL_SECS = 60
BACKOFF_START_SECS = 60
BACKOFF_MAX_SECS = 600


def in_monitor_window(now_et) -> bool:
    """Regular session only: weekdays 09:30-16:00 ET."""
    return now_et.weekday() < 5 and dtime(9, 30) <= now_et.time() < dtime(16, 0)


def is_rate_limited(err) -> bool:
    text = str(err).lower()
    return "429" in text or "rate limit" in text or "too many requests" in text


class Backoff:
    """Extra wait after a 429: 60 s, doubling, capped; reset on success."""

    def __init__(self, start=BACKOFF_START_SECS, cap=BACKOFF_MAX_SECS):
        self.start, self.cap, self.current = start, cap, 0

    def hit(self) -> int:
        self.current = self.start if not self.current \
            else min(self.current * 2, self.cap)
        return self.current

    def reset(self):
        self.current = 0


def held_bot_tickers(positions: dict) -> list:
    return sorted(t for t, s in positions.items()
                  if s.get("in_position") and s.get("source") == "bot")


def monitor_tick(broker, positions, lock, now_et, manage, interval_mins=5):
    """One pass. Returns the number of positions managed.

    `manage(broker, positions, ticker, df, broker_symbols, now_et)` does the
    per-position work and is called with `lock` held. Raises the broker's
    error on a failed fetch so the caller can back off."""
    with lock:
        tickers = held_bot_tickers(positions)
    if not tickers:
        return 0
    bars = broker.get_bars(tickers, timeframe_minutes=interval_mins,
                           lookback_days=2)
    try:
        broker_symbols = {p.symbol for p in broker.get_positions()}
    except Exception as e:
        if is_rate_limited(e):
            raise
        broker_symbols = None        # unknown: don't infer exits this pass
    managed = 0
    for ticker in tickers:
        df = bars.get(ticker)
        if df is None or getattr(df, "empty", True):
            continue
        with lock:
            # Re-read under the lock: the main cycle may have exited it.
            if not positions.get(ticker, {}).get("in_position"):
                continue
            manage(broker, positions, ticker, df, broker_symbols, now_et)
            managed += 1
    return managed


def run_monitor(broker, positions, lock, manage, should_run, now_et_fn,
                interval_mins_fn=lambda: 5, sleep=time.sleep,
                interval=MONITOR_INTERVAL_SECS):
    """The loop. `should_run()` is checked before every pass (the worker's
    lock file and stop event); outside the session window it only sleeps."""
    backoff = Backoff()
    while should_run():
        now_et = now_et_fn()
        wait = interval
        if in_monitor_window(now_et):
            try:
                monitor_tick(broker, positions, lock, now_et, manage,
                             interval_mins_fn())
                backoff.reset()
            except Exception as e:
                if is_rate_limited(e):
                    wait = interval + backoff.hit()
                    logger.warning(f"position monitor: rate limited — backing "
                                   f"off {wait}s ({e})")
                else:
                    logger.warning(f"position monitor pass failed: {e}")
        sleep(wait)


def start_monitor_thread(**kwargs) -> threading.Thread:
    thread = threading.Thread(target=run_monitor, kwargs=kwargs,
                              name="position-monitor", daemon=True)
    thread.start()
    return thread
