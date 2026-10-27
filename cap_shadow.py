"""
cap_shadow.py — S56b paper twin with the OLD 3.0 x ATR14 target cap
(CEO 2026-10-27: the live cap became 1.5 x ATR14 for new entries from
2026-10-28, agenda 37; the twin shows what keeping 3.0 would have made).

PAPER ONLY - no order code, never imported by the worker, own journal table
(cap_shadow, source='cap_shadow'), never decisions/trades.

Every live daily position entered from 2026-10-28 gets a TWIN that differs
ONLY in its target: fill + min(3R, 3.0 x ATR14) instead of the live
fill + min(3R, 1.5 x ATR14). The twin's target is never lower, so:
  * open          - the live position is open: the twin is the same trade;
  * same_as_live  - the live trade closed other than at its target (stop,
                    time stop, owner): price never reached either target, so
                    the twin exits with it, same time, same price;
  * twin_running  - the live trade sold at its 1.5 x ATR target: the twin
                    keeps going under the live rules (stop, ladder on 5-minute
                    closes, time stops - the S56 replay simulator, resumed
                    from the bar after the live exit with the live state);
  * twin_closed   - ... and has since exited (its 3.0 target, a stop or a
                    time stop).
If 3R is the binding limit (3R < 1.5 x ATR) both targets are equal: same.
Same shares as the live trade, so the $ difference is the cap's effect.
Run by the dip-test job (dip_shadow.py) every 5 minutes; its 16:20 summary
and the weekly scoreboard ("3.0CAP" column) carry it. CEO review after
REVIEW_AFTER closed trades.
"""

import json
import sqlite3

START = "2026-10-28"                 # first day of the live 1.5 cap
CAP_ATR, RR = 3.0, 3.0               # the twin: the OLD cap
REVIEW_AFTER = 20
SOURCE = "cap_shadow"
OPEN, SAME, RUNNING, CLOSED = "open", "same_as_live", "twin_running", "twin_closed"
DONE = (SAME, CLOSED)

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
    """fill + min(3R, 3.0 x ATR14) - the live formula with the OLD cap."""
    import position_mgmt as pm
    return pm.capped_target(fill, initial_stop, atr14, CAP_ATR, RR)


def live_book(conn, start=START):
    """Every BUY from `start` with its first later SELL of the ticker (the
    live exit): time, price, reason and the stop at exit."""
    out = []
    for b in conn.execute("SELECT id, timestamp, ticker, qty, price FROM trades "
                          "WHERE action='BUY' AND substr(timestamp,1,10) >= ? ORDER BY id",
                          (start,)).fetchall():
        sell = conn.execute("SELECT timestamp, price, reason, exit_stop FROM trades WHERE "
                            "action='SELL' AND ticker=? AND id>? ORDER BY id LIMIT 1",
                            (b[2], b[0])).fetchone()
        out.append({"trade_id": b[0], "entry_ts": b[1], "ticker": b[2], "qty": float(b[3]),
                     "fill": float(b[4]),
                     "live_exit_ts": sell[0] if sell else None,
                     "live_exit_px": float(sell[1]) if sell else None,
                     "live_exit_reason": sell[2] if sell else None,
                     "live_exit_stop": (float(sell[3]) if sell and sell[3] is not None else None)})
    return out


def _ts(x):
    import pandas as pd
    t = pd.Timestamp(x)
    return t.tz_localize("America/New_York") if t.tzinfo is None else t


def live_exit_at_target(reason, exit_px, live_target) -> bool:
    if str(reason or "").startswith("Profit Target"):
        return True
    return bool(live_target and exit_px is not None and exit_px >= live_target - 0.01)


def evaluate(bars, daily_close, atr_day, row, live, steps):
    """The twin's state. `row` = the cap_shadow row (fill, qty, initial_stop,
    live_target, twin_target, entry_ts); `live` = its live_book entry."""
    import pandas as pd
    import study_exit_grid as g
    fill, stop0 = row["fill"], row["initial_stop"]
    risk = fill - stop0
    last = float(bars["close"].iloc[-1]) if not bars.empty else None
    out = {"status": OPEN, "twin_exit_ts": None, "twin_exit_px": None, "mark_px": last}
    if live["live_exit_ts"] is None:
        return out                                        # the same open trade
    lx, lpx = _ts(live["live_exit_ts"]), live["live_exit_px"]
    same = {"status": SAME, "twin_exit_ts": str(live["live_exit_ts"]), "twin_exit_px": lpx,
            "mark_px": lpx}
    if (not live_exit_at_target(live["live_exit_reason"], lpx, row["live_target"])
            or row["twin_target"] is None or row["live_target"] is None
            or row["twin_target"] <= row["live_target"] + 0.005):
        return same
    entry = _ts(row["entry_ts"])
    held = bars[(bars.index > entry.floor("5min")) & (bars.index <= lx.floor("5min"))]
    after = bars[bars.index > lx.floor("5min")]
    if after.empty:
        return {"status": RUNNING, "twin_exit_ts": None, "twin_exit_px": None, "mark_px": lpx}
    q = held[[(t.hour, t.minute) >= (10, 0) for t in held.index]]
    hc0 = max([fill] + [float(c) for c in q["close"]])
    mfe0 = max([0.0] + [(float(h) - fill) / risk for h in held["high"]])
    ladder = [fill + sr * risk for t, sr in steps if (hc0 - fill) / risk >= t]
    start_stop = max([stop0] + ladder + ([live["live_exit_stop"]] if live["live_exit_stop"] else []))
    entry_date = entry.date()
    first_day = after.index[0].date()
    sessions_before = len({t.date() for t in bars.index if entry_date <= t.date() < first_day})
    res = g.simulate_hold(after, daily_close, atr_day, pd.Timestamp(after.index[0]), fill, stop0,
                          row["twin_target"], steps, None, start_stop=start_stop, hc0=hc0,
                          mfe0=mfe0, sessions_before=sessions_before, entry_date=entry_date,
                          continuing=True)
    if res is None:
        return {"status": RUNNING, "twin_exit_ts": None, "twin_exit_px": None,
                "mark_px": float(after["close"].iloc[-1])}
    r, _why, _mfe, xts = res
    px = round(fill + r * risk, 4)
    return {"status": CLOSED, "twin_exit_ts": str(xts), "twin_exit_px": px, "mark_px": px}


def update(conn, data, now, positions_file="positions.json"):
    """Register new live trades (ATR14 + initial stop from the live state
    while the position is open) and evaluate every unfinished twin."""
    import pandas as pd
    import position_mgmt as pm
    import study_exit_grid as g
    try:
        with open(positions_file, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    book = {t["trade_id"]: t for t in live_book(conn)}
    known = {r["trade_id"] for r in conn.execute("SELECT trade_id FROM cap_shadow")}
    for t in book.values():
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
    rows = [dict(r) for r in conn.execute("SELECT * FROM cap_shadow WHERE status IN (?,?)",
                                          (OPEN, RUNNING))]
    if not rows:
        return {"twins": len(known), "updated": 0}
    tickers = sorted({r["ticker"] for r in rows})
    m5 = data.m5(tickers, min(r["entry_ts"][:10] for r in rows))
    daily = data.daily(tickers, days=120)
    steps = [tuple(x) for x in pm.r_atr_params(g._live_profile())["steps"]]
    n = 0
    for r in rows:
        b, d = m5.get(r["ticker"]), daily.get(r["ticker"])
        live = book.get(r["trade_id"])
        if b is None or b.empty or live is None:
            continue
        closes = ({pd.Timestamp(i).date(): float(v) for i, v in d["close"].items()}
                  if d is not None and not d.empty else {})
        atrs = g.atr_by_day(d) if d is not None and len(d) > 15 else {}
        res = evaluate(b, closes, atrs, r, live, steps)
        live_px = live["live_exit_px"] if live["live_exit_px"] is not None else res["mark_px"]
        twin_px = res["twin_exit_px"] if res["twin_exit_px"] is not None else res["mark_px"]
        conn.execute("UPDATE cap_shadow SET status=?, twin_exit_ts=?, twin_exit_px=?, live_exit_ts=?, "
                     "live_exit_px=?, mark_px=?, live_pnl=?, twin_pnl=?, updated_at=? WHERE trade_id=?",
                     (res["status"], res["twin_exit_ts"], res["twin_exit_px"], live["live_exit_ts"],
                      live["live_exit_px"], res["mark_px"],
                      None if live_px is None else round(r["qty"] * (live_px - r["fill"]), 2),
                      None if twin_px is None else round(r["qty"] * (twin_px - r["fill"]), 2),
                      stamp, r["trade_id"]))
        n += 1
    conn.commit()
    return {"twins": len(known), "updated": n}


def rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM cap_shadow ORDER BY trade_id")]


def by_entry(conn) -> dict:
    """{(ticker, entry_ts): row} for the weekly scoreboard; {} if no table."""
    try:
        return {(r["ticker"], r["entry_ts"]): r for r in rows(conn)}
    except sqlite3.Error:
        return {}


def summary_line(rs) -> str:
    if not rs:
        return f"🎯 Old 3.0×ATR cap twin (S56b, PAPER): no live trades since {START} yet"
    live = sum(r["live_pnl"] or 0 for r in rs)
    twin = sum(r["twin_pnl"] or 0 for r in rs)
    done = sum(r["status"] in DONE for r in rs)
    review = f" · REVIEW DUE ({done} closed)" if done >= REVIEW_AFTER else f" · review at {done}/{REVIEW_AFTER} closed"
    return (f"🎯 Old 3.0×ATR cap twin (S56b, PAPER): {len(rs)} trade(s) · live (1.5 cap) "
            f"${live:+,.2f} vs 3.0 cap ${twin:+,.2f} (3.0 - live ${twin - live:+,.2f}){review}")
