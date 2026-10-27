"""
cap_shadow.py — S56b paper side-by-side of the 1.5 x ATR14 target cap
(CEO 2026-10-27, after S56 found it a near miss: +0.010R, MaxDD 67%).

PAPER ONLY - no order code, never imported by the worker, own journal table
(cap_shadow, source='cap_shadow'), never decisions/trades.

Every live daily position from 2026-10-27 (and any still open that day) gets
a TWIN that differs ONLY in its target: fill + min(3R, 1.5 x ATR14) instead
of the live fill + min(3R, 3 x ATR14). Until the twin's lower target is
touched, both have the same stop, ladder and time stops, so:
  * twin_target  - a 5-minute high reached the twin target first: the twin
                   sells there (from the bar AFTER the entry bar);
  * same_as_live - the live position closed first: the twin exits with it,
                   same time, same price;
  * open         - neither yet: marked at the last price.
Same shares as the live trade, so the $ difference is the cap's effect.
Run by the dip-test job (dip_shadow.py) every 5 minutes; its 16:20 summary
carries one line for this test.
"""

import json
import sqlite3

START = "2026-10-27"
CAP_ATR, RR = 1.5, 3.0
SOURCE = "cap_shadow"
OPEN, TWIN_TARGET, SAME = "open", "twin_target", "same_as_live"

SCHEMA = """CREATE TABLE IF NOT EXISTS cap_shadow (
    trade_id INTEGER PRIMARY KEY,           -- the live BUY row in trades
    source TEXT NOT NULL DEFAULT 'cap_shadow',
    ticker TEXT NOT NULL, entry_ts TEXT NOT NULL, fill REAL NOT NULL, qty REAL NOT NULL,
    initial_stop REAL, atr14 REAL, live_target REAL, twin_target REAL,
    status TEXT NOT NULL DEFAULT 'open',
    twin_exit_ts TEXT, twin_exit_px REAL, live_exit_ts TEXT, live_exit_px REAL,
    mark_px REAL, live_pnl REAL, twin_pnl REAL, updated_at TEXT)"""


def connect(db=None):
    import journal
    conn = sqlite3.connect(db or journal.DB_FILE, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    return conn


def twin_target(fill, initial_stop, atr14):
    """fill + min(3R, 1.5 x ATR14) - the live formula with the 1.5 cap."""
    import position_mgmt as pm
    return pm.capped_target(fill, initial_stop, atr14, CAP_ATR, RR)


def live_book(conn, start=START):
    """[{trade_id, ticker, entry_ts, fill, qty, live_exit_ts, live_exit_px}] for
    every BUY from `start`, plus earlier BUYs still open at `start`. The exit
    is the first SELL of the ticker after the BUY."""
    buys = conn.execute("SELECT id, timestamp, ticker, qty, price FROM trades "
                        "WHERE action='BUY' ORDER BY id").fetchall()
    out = []
    for b in buys:
        sell = conn.execute("SELECT timestamp, price FROM trades WHERE action='SELL' AND ticker=? "
                            "AND id>? ORDER BY id LIMIT 1", (b[2], b[0])).fetchone()
        if b[1][:10] < start and (sell is not None and sell[0][:10] < start):
            continue                                   # opened and closed before the test
        out.append({"trade_id": b[0], "entry_ts": b[1], "ticker": b[2], "qty": float(b[3]),
                    "fill": float(b[4]),
                    "live_exit_ts": sell[0] if sell else None,
                    "live_exit_px": float(sell[1]) if sell else None})
    return out


def evaluate(bars, entry_ts, fill, qty, twin_tgt, live_exit_ts=None, live_exit_px=None):
    """The twin's outcome from 5-minute bars (ET index) from the entry day on."""
    import pandas as pd
    entry = pd.Timestamp(entry_ts)
    if entry.tzinfo is None:
        entry = entry.tz_localize("America/New_York")
    start_bar = entry.floor("5min")
    after = bars[bars.index > start_bar]
    if live_exit_ts is not None:
        lx = pd.Timestamp(live_exit_ts)
        lx = lx.tz_localize("America/New_York") if lx.tzinfo is None else lx
        after = after[after.index < lx.floor("5min") + pd.Timedelta(minutes=5)]
    for ts, r in after.iterrows():
        if twin_tgt is not None and float(r["high"]) >= twin_tgt:
            if live_exit_ts is not None and ts >= pd.Timestamp(lx).floor("5min"):
                break                                  # same bar as the live exit: live first
            return {"status": TWIN_TARGET, "twin_exit_ts": str(ts), "twin_exit_px": twin_tgt,
                    "twin_pnl": round(qty * (twin_tgt - fill), 2)}
    if live_exit_ts is not None:
        if twin_tgt is not None and live_exit_px is not None and live_exit_px >= twin_tgt:
            # the live trade sold at or above the twin target: price went through it
            return {"status": TWIN_TARGET, "twin_exit_ts": str(live_exit_ts), "twin_exit_px": twin_tgt,
                    "twin_pnl": round(qty * (twin_tgt - fill), 2)}
        return {"status": SAME, "twin_exit_ts": str(live_exit_ts), "twin_exit_px": live_exit_px,
                "twin_pnl": round(qty * (live_exit_px - fill), 2)}
    return {"status": OPEN, "twin_exit_ts": None, "twin_exit_px": None, "twin_pnl": None}


def update(conn, data, now, positions_file="positions.json"):
    """Register new live trades (ATR14 + initial stop from the live state
    while the position is open) and evaluate every twin."""
    try:
        with open(positions_file, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    book = live_book(conn)
    known = {r["trade_id"]: dict(r) for r in conn.execute("SELECT * FROM cap_shadow")}
    for t in book:
        if t["trade_id"] in known:
            continue
        s = state.get(t["ticker"]) or {}
        if s.get("entry_trade_id") != t["trade_id"] or s.get("timeframe") != "daily":
            continue                                   # geometry only from the live state
        atr = s.get("target_cap_atr14") or s.get("atr14")
        stop0 = s.get("original_stop", s.get("initial_stop"))
        if not atr or stop0 is None:
            continue
        conn.execute("INSERT OR IGNORE INTO cap_shadow (trade_id, source, ticker, entry_ts, fill, "
                     "qty, initial_stop, atr14, live_target, twin_target, updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (t["trade_id"], SOURCE, t["ticker"], t["entry_ts"], t["fill"], t["qty"],
                      float(stop0), float(atr), s.get("profit_target_price"),
                      twin_target(t["fill"], float(stop0), float(atr)), stamp))
    conn.commit()
    rows = [dict(r) for r in conn.execute("SELECT * FROM cap_shadow WHERE status=?", (OPEN,))]
    if not rows:
        return {"twins": len(known), "updated": 0}
    by_id = {t["trade_id"]: t for t in book}
    first = min(r["entry_ts"][:10] for r in rows)
    bars = data.m5(sorted({r["ticker"] for r in rows}), first)
    n = 0
    for r in rows:
        b = bars.get(r["ticker"])
        t = by_id.get(r["trade_id"]) or {}
        if b is None or b.empty:
            continue
        res = evaluate(b, r["entry_ts"], r["fill"], r["qty"], r["twin_target"],
                       t.get("live_exit_ts"), t.get("live_exit_px"))
        last = float(b["close"].iloc[-1])
        live_px = t.get("live_exit_px") if t.get("live_exit_px") is not None else last
        twin_px = res["twin_exit_px"] if res["twin_exit_px"] is not None else last
        conn.execute("UPDATE cap_shadow SET status=?, twin_exit_ts=?, twin_exit_px=?, live_exit_ts=?, "
                     "live_exit_px=?, mark_px=?, live_pnl=?, twin_pnl=?, updated_at=? WHERE trade_id=?",
                     (res["status"], res["twin_exit_ts"], res["twin_exit_px"], t.get("live_exit_ts"),
                      t.get("live_exit_px"), last, round(r["qty"] * (live_px - r["fill"]), 2),
                      round(r["qty"] * (twin_px - r["fill"]), 2), stamp, r["trade_id"]))
        n += 1
    conn.commit()
    return {"twins": len(known), "updated": n}


def rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM cap_shadow ORDER BY trade_id")]


def summary_line(rs) -> str:
    if not rs:
        return f"🎯 1.5×ATR target twin (S56b, PAPER): no live trades since {START} yet"
    live = sum(r["live_pnl"] or 0 for r in rs)
    twin = sum(r["twin_pnl"] or 0 for r in rs)
    hits = sum(r["status"] == TWIN_TARGET for r in rs)
    return (f"🎯 1.5×ATR target twin (S56b, PAPER): {len(rs)} trade(s), twin target hit {hits} · "
            f"live ${live:+,.2f} vs twin ${twin:+,.2f} (diff ${twin - live:+,.2f})")
