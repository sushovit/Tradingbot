"""
clockline.py — dual-timezone, self-dating report headers.

One line, used by every report generator (report.py, universe.py, floor.py,
intern_desk.py, snapshot.py):

    2026-07-23 15:17 ET  |  2026-07-24 01:02 Nepal  |  US market: OPEN (closes in 0h43m)

Nepal is Asia/Kathmandu via zoneinfo — the :45 offset is handled by the tz
database, never hand-rolled. The market-session label comes from the ET
clock + NYSE weekends/holidays.

annotate_age() is applied at PRINT time (scan.py / snapshot.py): it finds
header stamps in already-generated text and appends "generated Xm ago", so
a stale file self-identifies whenever it is read.
"""

import re
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
NEPAL = ZoneInfo("Asia/Kathmandu")

MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)

# NYSE full-closure days, 2026. Extend yearly.
NYSE_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}


def now_et() -> datetime:
    return datetime.now(ET)


def is_trading_day(d) -> bool:
    return d.weekday() < 5 and d.strftime("%Y-%m-%d") not in NYSE_HOLIDAYS


def _next_open(after_et: datetime) -> datetime:
    """The next NYSE open strictly after `after_et`."""
    candidate = after_et.replace(hour=9, minute=30, second=0, microsecond=0)
    if after_et.time() >= MARKET_OPEN or not is_trading_day(after_et.date()):
        candidate += timedelta(days=1)
    while not is_trading_day(candidate.date()):
        candidate += timedelta(days=1)
    return candidate


def market_session_label(dt_et: datetime = None) -> str:
    dt_et = dt_et or now_et()
    if is_trading_day(dt_et.date()) and MARKET_OPEN <= dt_et.time() < MARKET_CLOSE:
        close_dt = dt_et.replace(hour=16, minute=0, second=0, microsecond=0)
        mins = int((close_dt - dt_et).total_seconds() // 60)
        return f"OPEN (closes in {mins // 60}h{mins % 60:02d}m)"
    nepal_open = _next_open(dt_et).astimezone(NEPAL)
    return f"CLOSED (opens {nepal_open:%a %H:%M} Nepal)"


def two_zone_line(dt_et: datetime = None) -> str:
    """The standard header line: ET | Nepal | session state."""
    dt_et = dt_et or now_et()
    nepal = dt_et.astimezone(NEPAL)
    return (f"{dt_et:%Y-%m-%d %H:%M} ET  |  {nepal:%Y-%m-%d %H:%M} Nepal  |  "
            f"US market: {market_session_label(dt_et)}")


_STAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) ET")


def _age_text(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}m ago"
    return f"{minutes // 60}h {minutes % 60:02d}m ago"


def annotate_age(text: str, now: datetime = None) -> str:
    """Append '| generated Xm ago' to every line carrying an ET header stamp.
    Computed at PRINT/read time so stale content self-identifies. Lines
    already annotated are left alone; unparseable stamps are left alone."""
    now = now or now_et()
    out = []
    for line in text.splitlines():
        m = _STAMP_RE.search(line)
        if m and "generated" not in line:
            try:
                stamp = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=ET)
                minutes = max(0, int((now - stamp).total_seconds() // 60))
                line = f"{line}  |  generated {_age_text(minutes)}"
            except ValueError:
                pass
        out.append(line)
    return "\n".join(out)
