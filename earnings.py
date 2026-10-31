"""
earnings.py — Finnhub earnings-calendar gate for post_earnings_continuation.

Work order 2026-09-02 item 2. The backtest could only test the OBSERVABLE
SHADOW of an earnings beat (a >=5% gap on >=2x volume), which also catches
M&A pops, guidance raises and sector news. This module is what makes the
LIVE setup the earnings class: a gap only qualifies if an actual earnings
event landed within the prior N sessions.

FAIL CLOSED. If the calendar cannot be reached, `had_earnings_within`
returns None — "unknown", never False-as-if-checked and never True. The
caller must treat None as a skip and journal it. A setup defined by an
earnings event must not trade when we cannot confirm the event happened.

One market-wide call per day covers every ticker (259 rows for a 10-day
window on our key), so this costs one request per session, not one per
ticker.
"""

import datetime
import logging
import os
import threading

logger = logging.getLogger(__name__)

# How far back a gap may sit from the earnings print and still count.
DEFAULT_SESSIONS = 3
# Calendar days fetched around the window. Sessions are trading days, so a
# 3-session lookback can span a long weekend plus a holiday.
FETCH_PAD_DAYS = 12

_lock = threading.Lock()
_cache = {}          # {fetch_date_iso: {symbol: [date_iso, ...]}}
_failed = set()      # fetch dates whose call failed — do not retry all cycle


def _client():
    """Finnhub client, or None. Import is local so the module stays importable
    (and testable) on a box without the package or the key."""
    api_key = os.getenv("FINNHUB_API_KEY")
    if not api_key:
        return None
    try:
        import finnhub
        return finnhub.Client(api_key=api_key)
    except Exception as e:                      # pragma: no cover - env only
        logger.warning(f"Finnhub client unavailable: {e}")
        return None


def _fetch(asof: datetime.date, client=None) -> dict:
    """{SYMBOL: [earnings date iso, ...]} for the window ending at `asof`.
    Raises on transport failure — the caller decides the fail-closed policy."""
    client = client or _client()
    if client is None:
        raise RuntimeError("no Finnhub client (missing package or API key)")
    start = (asof - datetime.timedelta(days=FETCH_PAD_DAYS)).isoformat()
    end = asof.isoformat()
    payload = client.earnings_calendar(_from=start, to=end, symbol="")
    rows = (payload or {}).get("earningsCalendar") or []
    out = {}
    for row in rows:
        sym = str(row.get("symbol") or "").upper()
        day = str(row.get("date") or "")[:10]
        if sym and day:
            out.setdefault(sym, [])
            if day not in out[sym]:
                out[sym].append(day)
    return out


def calendar_for(asof: datetime.date = None, client=None):
    """Cached calendar map for `asof`, or None if the call failed.
    Cached per date: one request per session, not one per ticker."""
    asof = asof or datetime.date.today()
    key = asof.isoformat()
    with _lock:
        if key in _cache:
            return _cache[key]
        if key in _failed:
            return None
    try:
        data = _fetch(asof, client=client)
    except Exception as e:
        logger.warning(f"Earnings calendar unavailable for {key}: {e}")
        with _lock:
            _failed.add(key)
        return None
    with _lock:
        _cache[key] = data
    return data


def sessions_back(asof: datetime.date, sessions: int) -> datetime.date:
    """The date `sessions` TRADING days before `asof`, weekends excluded.

    Holidays are not excluded, which makes the window very slightly SHORTER
    than a true session count — the conservative direction for a gate that
    is supposed to prove an event happened recently."""
    day = asof
    remaining = sessions
    while remaining > 0:
        day -= datetime.timedelta(days=1)
        if day.weekday() < 5:
            remaining -= 1
    return day


def had_earnings_within(ticker: str, sessions: int = DEFAULT_SESSIONS,
                        asof: datetime.date = None, client=None):
    """True / False / None.

      True  — an earnings event for `ticker` dated in the `sessions` trading
              days ending at `asof` (inclusive).
      False — the calendar was read and holds no such event.
      None  — the calendar could not be read. FAIL CLOSED: the caller must
              skip the setup and journal 'earnings_calendar_unavailable'.
    """
    asof = asof or datetime.date.today()
    data = calendar_for(asof, client=client)
    if data is None:
        return None
    floor = sessions_back(asof, sessions)
    for day in data.get(str(ticker or "").upper(), []):
        try:
            when = datetime.date.fromisoformat(day)
        except ValueError:
            continue
        if floor <= when <= asof:
            return True
    return False


def reset_cache():
    """Test hook / new session: drop the cached calendar and failure marks."""
    with _lock:
        _cache.clear()
        _failed.clear()
        _upcoming_cache.clear()
        _upcoming_failed.clear()
        _range_cache.clear()
        _range_failed.clear()


# --- Upcoming earnings, for the daily-position exit (S25) -------------------
_upcoming_cache = {}     # {asof_iso: {SYMBOL: [(date_iso, hour), ...]}}
_upcoming_failed = set()


def _fetch_upcoming(asof: datetime.date, client=None) -> dict:
    """{SYMBOL: [(date iso, hour), ...]} for asof .. asof + 5 days. `hour`
    is Finnhub's bmo / amc / dmh, or '' when not given."""
    client = client or _client()
    if client is None:
        raise RuntimeError("no Finnhub client (missing package or API key)")
    end = (asof + datetime.timedelta(days=5)).isoformat()
    payload = client.earnings_calendar(_from=asof.isoformat(), to=end, symbol="")
    out = {}
    for row in (payload or {}).get("earningsCalendar") or []:
        sym = str(row.get("symbol") or "").upper()
        day = str(row.get("date") or "")[:10]
        if sym and day:
            out.setdefault(sym, []).append((day, str(row.get("hour") or "").lower()))
    return out


def next_session(asof: datetime.date) -> datetime.date:
    day = asof + datetime.timedelta(days=1)
    while day.weekday() >= 5:
        day += datetime.timedelta(days=1)
    return day


def reports_within_one_session(ticker: str, asof: datetime.date = None,
                               client=None):
    """True / False / None (calendar unreachable - the caller must NOT exit
    on unknown, and should journal it).

    True when the company reports after today's close (date today and hour
    amc, or no hour given) or at/before the next session (date = next
    session, any hour) - i.e. holding through today's close would carry the
    position through the print."""
    asof = asof or datetime.date.today()
    key = asof.isoformat()
    with _lock:
        cached = _upcoming_cache.get(key)
        failed = key in _upcoming_failed
    if cached is None:
        if failed:
            return None
        try:
            cached = _fetch_upcoming(asof, client=client)
        except Exception as e:
            logger.warning(f"Upcoming-earnings calendar unavailable for {key}: {e}")
            with _lock:
                _upcoming_failed.add(key)
            return None
        with _lock:
            _upcoming_cache[key] = cached
    nxt = next_session(asof).isoformat()
    for day, hour in cached.get(str(ticker or "").upper(), []):
        if day == key and hour in ("amc", ""):
            return True
        if day == nxt:
            return True
    return False


# --- Date-range calendar (CEO 2026-10-31, agenda 39) -------------------------
# The earnings-gap cooldown needs reports up to ~6 weeks back and the
# gatekeeper's "earnings-ahead" group the next two weeks. One market-wide call
# per (start, end) window, cached; a failed window is not retried that day.
_range_cache = {}        # {(start_iso, end_iso): {SYMBOL: [date_iso, ...]}}
_range_failed = set()


def calendar_range(start: datetime.date, end: datetime.date, client=None):
    """{SYMBOL: [date iso, ...]} for start..end inclusive, or None if the
    calendar could not be read (callers decide their fail policy)."""
    key = (start.isoformat(), end.isoformat())
    with _lock:
        if key in _range_cache:
            return _range_cache[key]
        if key in _range_failed:
            return None
    try:
        client = client or _client()
        if client is None:
            raise RuntimeError("no Finnhub client (missing package or API key)")
        payload = client.earnings_calendar(_from=key[0], to=key[1], symbol="")
        out = {}
        for row in (payload or {}).get("earningsCalendar") or []:
            sym = str(row.get("symbol") or "").upper()
            day = str(row.get("date") or "")[:10]
            if sym and day and day not in out.setdefault(sym, []):
                out[sym].append(day)
    except Exception as e:
        logger.warning(f"Earnings calendar range {key} unavailable: {e}")
        with _lock:
            _range_failed.add(key)
        return None
    with _lock:
        _range_cache[key] = out
    return out


def dates_for(ticker: str, start: datetime.date, end: datetime.date, client=None):
    """Sorted earnings dates of `ticker` in start..end, or None (unknown)."""
    data = calendar_range(start, end, client=client)
    if data is None:
        return None
    return sorted(data.get(str(ticker or "").upper(), []))
