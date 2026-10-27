"""
dip_shadow.py — S55 forward shadow test of the owner's dip idea (CEO 2026-10-27).

PAPER ONLY. Nothing here can place an order: this module imports no order
code, the worker never imports it, and it writes only its own journal table
(dip_shadow, source='dip_shadow') - never `decisions`/`trades`, so no trade
statistic, goal, review or the gatekeeper ever sees these rows.

The rule (entries 2026-10-28 .. 2026-11-06, regular session, IEX 5-minute bars):
  * universe: the scanner universe, prior close >= $50;
  * the day's open within +-1% of the prior close;
  * the FIRST touch of prior close - 2% in a bar starting before 11:00 ET
    -> paper buy at that level (prior close x 0.98);
  * exit: back to the prior close the same day (hit_same_day), or within the
    next 3 sessions (hit_3d), else marked at the day-3 close (day3_close);
    worst drawdown = the lowest low from the touch bar to the exit.
  * control: for each dip, a paper buy of the same stock at 09:45 that day
    (the 09:45 bar's open) with the SAME exit (same moment, same price), so
    the pair differs only in the entry.
A hit is counted from the bar AFTER the touch bar (inside one 5-minute bar the
order of high and low is unknown - conservative).

    python dip_shadow.py --update          # every 5 min (launchd); gated by clock
    python dip_shadow.py --update --post   # 16:20 ET: final update + Discord summary
    python dip_shadow.py --show            # print the table and totals
"""

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timedelta

ET_NAME = "America/New_York"
START, LAST_ENTRY = "2026-10-28", "2026-11-06"
MIN_PRICE, OPEN_BAND, DIP = 50.0, 0.01, 0.02
CUTOFF_MIN = 11 * 60                 # touch bar must START before 11:00 ET
CONTROL_MIN = 9 * 60 + 45            # control: the 09:45 bar's open
HOLD_SESSIONS = 3                    # sessions after the entry day
SOURCE = "dip_shadow"
OPEN, HIT0, HIT3, DAY3 = "open", "hit_same_day", "hit_3d", "day3_close"

logger = logging.getLogger(__name__)

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS dip_shadow (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL DEFAULT 'dip_shadow',
        day TEXT NOT NULL, ticker TEXT NOT NULL,
        prior_close REAL NOT NULL, day_open REAL NOT NULL, level REAL NOT NULL,
        entry_ts TEXT NOT NULL, control_px REAL,
        status TEXT NOT NULL DEFAULT 'open', sessions INTEGER DEFAULT 1,
        exit_ts TEXT, exit_px REAL, mark_px REAL,
        ret_pct REAL, control_ret_pct REAL,
        worst_dd_pct REAL, control_worst_dd_pct REAL,
        updated_at TEXT, UNIQUE (day, ticker))""",
    """CREATE TABLE IF NOT EXISTS dip_shadow_watch (
        day TEXT NOT NULL, ticker TEXT NOT NULL,
        prior_close REAL NOT NULL, day_open REAL NOT NULL,
        PRIMARY KEY (day, ticker))""",
)


def _db():
    import journal
    return journal.DB_FILE


def connect(db=None):
    conn = sqlite3.connect(db or _db(), timeout=15)
    conn.row_factory = sqlite3.Row
    for s in SCHEMA:
        conn.execute(s)
    return conn


# ===================================================================== rules (pure)

def qualifies(prior_close, day_open) -> bool:
    return (prior_close is not None and day_open is not None and prior_close >= MIN_PRICE
            and abs(day_open / prior_close - 1.0) <= OPEN_BAND + 1e-9)


def level_for(prior_close) -> float:
    return round(prior_close * (1.0 - DIP), 2)


def _mins(ts) -> int:
    return ts.hour * 60 + ts.minute


def find_touch(day_bars, level):
    """Start time of the first bar starting before 11:00 whose low <= level."""
    for ts, b in day_bars.iterrows():
        if _mins(ts) >= CUTOFF_MIN:
            return None
        if float(b["low"]) <= level:
            return ts
    return None


def control_price(day_bars):
    """(ts, open) of the first bar at/after 09:45, or None."""
    for ts, b in day_bars.iterrows():
        if _mins(ts) >= CONTROL_MIN:
            return ts, float(b["open"])
    return None


def evaluate(bars, entry_ts, level, prior_close, control_px=None, complete_through=None):
    """Outcome of one dip from 5-minute RTH bars (ET index) covering the entry
    day onward. `complete_through` = the last date whose session is over
    (bars after it are today's partial session). Returns a dict of the
    columns to store."""
    import pandas as pd
    entry_ts = pd.Timestamp(entry_ts)
    b = bars[bars.index >= entry_ts]
    days = sorted({t.date() for t in b.index})[:HOLD_SESSIONS + 1]
    b = b[[t.date() in set(days) for t in b.index]]
    out = {"status": OPEN, "sessions": len(days), "exit_ts": None, "exit_px": None,
           "mark_px": None, "ret_pct": None, "control_ret_pct": None,
           "worst_dd_pct": None, "control_worst_dd_pct": None}
    if b.empty:
        return out
    low = float(b["low"].iloc[0])
    exit_ts = exit_px = None
    for ts, r in b.iloc[1:].iterrows():
        low = min(low, float(r["low"]))
        if float(r["high"]) >= prior_close:
            exit_ts, exit_px = ts, prior_close
            out["status"] = HIT0 if ts.date() == days[0] else HIT3
            break
    if exit_ts is None:
        last_day = days[-1]
        done = (len(days) == HOLD_SESSIONS + 1 and complete_through is not None
                and last_day <= complete_through)
        if done:
            exit_ts, exit_px = b.index[-1], float(b["close"].iloc[-1])
            out["status"] = DAY3
    out["worst_dd_pct"] = round(100 * (low / level - 1.0), 3)
    mark = exit_px if exit_px is not None else float(b["close"].iloc[-1])
    out["mark_px"] = round(mark, 4)
    out["ret_pct"] = round(100 * (mark / level - 1.0), 3)
    if exit_ts is not None:
        out["exit_ts"], out["exit_px"] = str(exit_ts), round(exit_px, 4)
    if control_px:
        day0 = bars[[t.date() == days[0] for t in bars.index]]
        c = control_price(day0)
        if c and (exit_ts is None or exit_ts > c[0]):
            span = bars[(bars.index >= c[0]) & ((bars.index <= exit_ts) if exit_ts is not None
                                                 else (bars.index <= b.index[-1]))]
            out["control_ret_pct"] = round(100 * (mark / control_px - 1.0), 3)
            out["control_worst_dd_pct"] = round(100 * (float(span["low"].min()) / control_px - 1.0), 3)
    return out


# ===================================================================== data

class AlpacaData:
    """IEX bars through the Alpaca data API (read only)."""

    def __init__(self):
        from dotenv import load_dotenv
        load_dotenv(".env")
        from broker import Broker
        self.client = Broker().data

    def _bars(self, tickers, tf, start):
        import pandas as pd
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        if not tickers:
            return {}
        req = StockBarsRequest(symbol_or_symbols=sorted(tickers), timeframe=tf,
                               start=start, feed=DataFeed.IEX)
        df = self.client.get_stock_bars(req).df
        out = {}
        if df is None or df.empty:
            return out
        for sym in sorted(tickers):
            if sym not in df.index.get_level_values("symbol"):
                continue
            d = df.xs(sym, level="symbol").copy()
            d.index = pd.DatetimeIndex(d.index).tz_convert(ET_NAME)
            out[sym] = d[["open", "high", "low", "close", "volume"]]
        return out

    def daily(self, tickers, days=10):
        from alpaca.data.timeframe import TimeFrame
        start = datetime.utcnow() - timedelta(days=days)
        return self._bars(tickers, TimeFrame.Day, start)

    def m5(self, tickers, start_day):
        import pandas as pd
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        start = pd.Timestamp(str(start_day)).tz_localize(ET_NAME).tz_convert("UTC").to_pydatetime()
        out = self._bars(tickers, TimeFrame(5, TimeFrameUnit.Minute), start)
        return {s: d[(d.index.hour * 60 + d.index.minute >= 570) &
                      (d.index.hour * 60 + d.index.minute < 960)] for s, d in out.items()}


def universe_symbols(path="universe_today.json") -> list:
    import json
    try:
        with open(path, encoding="utf-8") as f:
            return sorted({c["symbol"] for c in json.load(f).get("candidates", [])
                           if c.get("symbol") and c["symbol"] != "SPY"})
    except (OSError, ValueError, KeyError, TypeError):
        return []


# ===================================================================== update

def update(now, data, conn, symbols=None, session_over=None):
    """One idempotent pass. `now` is ET-aware. Returns a summary dict."""
    import pandas as pd
    today = now.date()
    tstr = today.isoformat()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    done = {"watch": 0, "new": 0, "updated": 0}
    in_window = START <= tstr <= LAST_ENTRY
    after_open = _mins(now) >= 9 * 60 + 35
    # 1) today's watch list (once): prior close >= $50, open within +-1%
    if in_window and after_open:
        have = conn.execute("SELECT count(*) FROM dip_shadow_watch WHERE day=?", (tstr,)).fetchone()[0]
        if not have:
            syms = symbols if symbols is not None else universe_symbols()
            daily = data.daily(syms)
            prior = {}
            for s, d in daily.items():
                past = d[[t.date() < today for t in d.index]]
                if not past.empty:
                    prior[s] = float(past["close"].iloc[-1])
            cands = [s for s, pc in prior.items() if pc >= MIN_PRICE]
            bars = data.m5(cands, today)
            for s in cands:
                b = bars.get(s)
                if b is None or b.empty or b.index[0].date() != today or _mins(b.index[0]) != 570:
                    continue
                if qualifies(prior[s], float(b["open"].iloc[0])):
                    conn.execute("INSERT OR IGNORE INTO dip_shadow_watch VALUES (?,?,?,?)",
                                 (tstr, s, prior[s], float(b["open"].iloc[0])))
                    done["watch"] += 1
            conn.commit()
    # 2) first touches before 11:00 (checked until 11:10 so the 10:55 bar is complete)
    if in_window and after_open and _mins(now) <= CUTOFF_MIN + 10:
        watch = {r["ticker"]: r for r in conn.execute(
            "SELECT * FROM dip_shadow_watch WHERE day=? AND ticker NOT IN "
            "(SELECT ticker FROM dip_shadow WHERE day=?)", (tstr, tstr))}
        bars = data.m5(list(watch), today) if watch else {}
        for s, w in watch.items():
            b = bars.get(s)
            if b is None or b.empty:
                continue
            b = b[[t.date() == today for t in b.index]]
            lvl = level_for(w["prior_close"])
            ts = find_touch(b, lvl)
            if ts is None:
                continue
            conn.execute("INSERT OR IGNORE INTO dip_shadow (source, day, ticker, prior_close, "
                         "day_open, level, entry_ts, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                         (SOURCE, tstr, s, w["prior_close"], w["day_open"], lvl, str(ts), stamp))
            done["new"] += 1
        conn.commit()
    # 3) every open dip: control price, outcome so far
    open_rows = list(conn.execute("SELECT * FROM dip_shadow WHERE status=?", (OPEN,)))
    if open_rows:
        first = min(r["day"] for r in open_rows)
        bars = data.m5(sorted({r["ticker"] for r in open_rows}), first)
        over = session_over if session_over is not None else _mins(now) >= 16 * 60
        complete = today if over else today - timedelta(days=1)
        for r in open_rows:
            b = bars.get(r["ticker"])
            if b is None or b.empty:
                continue
            cpx = r["control_px"]
            if cpx is None:
                c = control_price(b[[t.date().isoformat() == r["day"] for t in b.index]])
                cpx = c[1] if c else None
            res = evaluate(b, pd.Timestamp(r["entry_ts"]), r["level"], r["prior_close"],
                           cpx, complete)
            conn.execute(
                "UPDATE dip_shadow SET control_px=?, status=?, sessions=?, exit_ts=?, exit_px=?, "
                "mark_px=?, ret_pct=?, control_ret_pct=?, worst_dd_pct=?, "
                "control_worst_dd_pct=?, updated_at=? WHERE id=?",
                (cpx, res["status"], res["sessions"], res["exit_ts"], res["exit_px"],
                 res["mark_px"], res["ret_pct"], res["control_ret_pct"], res["worst_dd_pct"],
                 res["control_worst_dd_pct"], stamp, r["id"]))
            done["updated"] += 1
        conn.commit()
    return done


# ===================================================================== report

def rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM dip_shadow ORDER BY day, entry_ts")]


def totals(rs) -> dict:
    closed = [r for r in rs if r["status"] != OPEN]
    paired = [r for r in closed if r["control_ret_pct"] is not None]
    n = len(closed)

    def avg(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 3) if xs else None
    return {
        "dips": len(rs), "open": len(rs) - n, "closed": n,
        "hit_same_day_pct": round(100 * sum(r["status"] == HIT0 for r in closed) / n, 1) if n else None,
        "hit_3d_pct": round(100 * sum(r["status"] in (HIT0, HIT3) for r in closed) / n, 1) if n else None,
        "avg_ret_pct": avg([r["ret_pct"] for r in closed]),
        "avg_worst_dd_pct": avg([r["worst_dd_pct"] for r in closed]),
        "paired": len(paired),
        "avg_dip_paired_pct": avg([r["ret_pct"] for r in paired]),
        "avg_control_pct": avg([r["control_ret_pct"] for r in paired]),
    }


def _p(x, sign=True):
    return "-" if x is None else (f"{x:+.2f}%" if sign else f"{x:.1f}%")


def summary_lines(rs, day: str) -> list:
    t = totals(rs)
    new = [r for r in rs if r["day"] == day]
    edge = (None if t["avg_dip_paired_pct"] is None or t["avg_control_pct"] is None
            else t["avg_dip_paired_pct"] - t["avg_control_pct"])
    lines = [f"🧪 Dip test (S55, PAPER - no orders) {day}: {len(new)} new dip(s)"
             + (": " + ", ".join(f"{r['ticker']} @{r['level']:.2f}" for r in new) if new else ""),
             f"Since {START}: {t['dips']} dips · {t['closed']} closed · {t['open']} open"]
    if t["closed"]:
        lines.append(f"Back to prior close: same day {_p(t['hit_same_day_pct'], False)} · "
                     f"within 3 sessions {_p(t['hit_3d_pct'], False)} · avg {_p(t['avg_ret_pct'])} · "
                     f"worst DD avg {_p(t['avg_worst_dd_pct'])}")
        lines.append(f"vs 09:45 control ({t['paired']} pairs): dip {_p(t['avg_dip_paired_pct'])} · "
                     f"control {_p(t['avg_control_pct'])} · edge "
                     f"{'-' if edge is None else f'{edge:+.2f} pp'}")
    return lines


def post(lines):
    import alerts
    import trade_alerts
    if not trade_alerts.enabled("dip_shadow"):
        return 0
    alerts.post_async("\n".join(lines))
    alerts.drain()
    return 1


def main(argv=None) -> int:
    import pytz
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--post", action="store_true")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args(argv)
    now = datetime.now(pytz.timezone(ET_NAME))
    conn = connect()
    try:
        if a.update:
            m = _mins(now)
            busy = (now.weekday() < 5 and 9 * 60 + 35 <= m <= 16 * 60 + 25)
            has_open = conn.execute("SELECT count(*) FROM dip_shadow WHERE status=?",
                                    (OPEN,)).fetchone()[0]
            if busy and (START <= now.date().isoformat() <= LAST_ENTRY or has_open):
                data = AlpacaData()
                from broker import Broker
                if Broker().is_open_today():
                    print(update(now, data, conn))
        rs = rows(conn)
        if a.post:
            post(summary_lines(rs, now.date().isoformat()))
        if a.show or a.post:
            print("\n".join(summary_lines(rs, now.date().isoformat())))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
