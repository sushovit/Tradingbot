"""
saturday_report.py — the Saturday CEO review report (CEO operating cadence,
ordered 2026-10-28, received 2026-10-31; in force until the 2027-01-15
go-live review). Read-only: journal, logs, broker reads. Discord + a copy in
reports/saturday_<date>.md.

  (a) go-live checklist: >= 30 closed trades, average > 0R, max drawdown < 5%,
      4 weeks of on-time starts, protection checks green
  (b) CEO orders vs main: every CEO order of the week (ceo_orders.json) with
      its commit or NOT DONE
  (c) graded conviction 60-69 approvals and sector_cap blocks (outcomes job)
  (d) shadow model: "shadow NO" vs "shadow YES" trade results, toward 20
  + the Claude credit line (and the 30-day no-top-up reminder)

    python saturday_report.py [--date YYYY-MM-DD] [--no-post]
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta

import pytz

ET = pytz.timezone("US/Eastern")
GO_LIVE = {"closed_trades": 30, "avg_r": 0.0, "max_dd_pct": 5.0, "weeks": 4}
WORKER_LOG = os.path.join("logs", "worker.log")
START_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2})\] INFO: worker start PID")
PROT_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) [\d:]+\] INFO: protection: (.*)$")
ON_TIME = (9, 30)
ORDERS_FILE = "ceo_orders.json"


def _ok(b):
    return "✅" if b else "❌"


# ===================================================================== (a) go-live

def trade_r(t):
    stop = t.get("orig_stop")
    if stop is None or t.get("pnl") is None or not t.get("qty") or float(stop) >= float(t["buy"]):
        return None
    return float(t["pnl"]) / (float(t["qty"]) * (float(t["buy"]) - float(stop)))


def max_dd_pct(equity) -> float:
    """Peak-to-trough % of an equity series (zeros = before funding, skipped)."""
    peak, dd = None, 0.0
    for v in equity:
        if not v:
            continue
        peak = v if peak is None else max(peak, v)
        dd = max(dd, 100.0 * (peak - v) / peak)
    return dd


def start_times(log_lines):
    """{date: (hour, minute)} of the FIRST worker start each day."""
    out = {}
    for ln in log_lines:
        m = START_RE.match(ln)
        if m and m.group(1) not in out:
            out[m.group(1)] = (int(m.group(2)), int(m.group(3)))
    return out


def protection_lines(log_lines, since: str):
    """[(date, text)] of protection-check lines on/after `since`."""
    out = []
    for ln in log_lines:
        m = PROT_RE.match(ln)
        if m and m.group(1) >= since:
            out.append((m.group(1), m.group(2)))
    return out


def golive_lines(closed, equity, sessions, starts, prot):
    rs = [r for r in (trade_r(t) for t in closed) if r is not None]
    avg_r = sum(rs) / len(rs) if rs else None
    dd = max_dd_pct(equity) if equity else None
    late = [d for d in sessions if d not in starts or starts[d] > ON_TIME]
    bad = [(d, t) for d, t in prot if "⚠️" in t]
    lines = ["**(a) Go-live checklist** (review 2027-01-15)",
             f"{_ok(len(closed) >= GO_LIVE['closed_trades'])} closed trades {len(closed)} / >= {GO_LIVE['closed_trades']}",
             f"{_ok(avg_r is not None and avg_r > GO_LIVE['avg_r'])} average "
             + ("-" if avg_r is None else f"{avg_r:+.2f}R") + f" over {len(rs)} trades / > 0R",
             f"{_ok(dd is not None and dd < GO_LIVE['max_dd_pct'])} max drawdown "
             + ("-" if dd is None else f"{dd:.1f}%") + f" / < {GO_LIVE['max_dd_pct']:g}%",
             f"{_ok(not late and len(sessions) > 0)} on-time starts (by 09:30 ET) "
             f"{len(sessions) - len(late)}/{len(sessions)} sessions, last {GO_LIVE['weeks']} weeks"
             + (f" - late/missing: {', '.join(d[5:] for d in late[:8])}" if late else ""),
             f"{_ok(not bad and len(prot) > 0)} protection checks: {len(prot) - len(bad)}/{len(prot)} green"
             + (f" - red: {', '.join(d[5:] for d, _ in bad[:6])}" if bad else "")]
    return lines


# ===================================================================== (b) orders

def orders_lines(orders, day: date, days: int = 7):
    lo = (day - timedelta(days=days)).isoformat()
    week = [o for o in orders if lo <= o["date"] <= day.isoformat()]
    lines = [f"**(b) CEO orders vs main** ({lo[5:]}..{day.isoformat()[5:]})"]
    for o in week:
        st = str(o.get("status", ""))
        mark = "✅" if re.match(r"^[0-9a-f]{7}", st) or st.startswith("ops") else "❌"
        lines.append(f"{mark} {o['date'][5:]} {o['order']} → {st}")
    if not week:
        lines.append("(no CEO orders recorded this week)")
    return lines


# ===================================================================== (c) graded

def graded(conn, reason_key, conv_lo=None, conv_hi=None):
    q = ("SELECT ro.outcome, ro.r_achieved, ro.resolved FROM rejection_outcomes ro "
         "JOIN decisions d ON d.id = ro.decision_id WHERE ro.reason_key = ?")
    args = [reason_key]
    if conv_lo is not None:
        q += " AND d.conviction_score BETWEEN ? AND ?"
        args += [conv_lo, conv_hi]
    try:
        rows = conn.execute(q, args).fetchall()
    except sqlite3.Error:
        rows = []
    res = [r for r in rows if r[2] and r[1] is not None]
    win = (100.0 * sum(1 for r in res if r[1] > 0) / len(res)) if res else None
    avg = (sum(r[1] for r in res) / len(res)) if res else None
    return {"n": len(rows), "resolved": len(res), "win": win, "avg_r": avg}


def graded_line(label, g):
    if not g["n"]:
        return f"{label}: none yet"
    return (f"{label}: n {g['n']} ({g['resolved']} resolved) · win "
            + ("-" if g["win"] is None else f"{g['win']:.0f}%")
            + " · avg " + ("-" if g["avg_r"] is None else f"{g['avg_r']:+.2f}R"))


def graded_lines(conn):
    return ["**(c) Graded blocks** (what the trade would have done; outcomes job)",
            graded_line("Conviction 60-69 approvals (not traded, < 70)",
                        graded(conn, "gatekeeper_low_conviction", 60, 69)),
            graded_line("sector_cap blocks (max 1 per sector)", graded(conn, "sector_cap"))]


# ===================================================================== (d) shadow

def shadow_report_lines(split):
    import scoreboard
    if not split:
        return ["**(d) Shadow model**", "(no data)"]
    return ["**(d) Shadow model** (verdict on the same signal)"] + [
        ln.strip() for ln in scoreboard.shadow_lines(split)[1:]]


# ===================================================================== assemble

def build(day: date, conn, broker, log_lines, orders, config):
    import credit
    import scoreboard
    import trade_table
    open_pos = trade_table.live_open_positions(broker) if broker else {}
    closed = [t for t in trade_table.load_trades(conn)
              if t["buy_ts"] >= trade_table.ACCOUNT_START
              and not str(t.get("setup") or "").upper().startswith("INTERN")
              and t["sell"] is not None and t["reason"] != "partial"]
    equity = []
    try:
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        h = broker.trading.get_portfolio_history(GetPortfolioHistoryRequest(period="3M",
                                                                            timeframe="1D"))
        equity = [float(v or 0) for v in (h.equity or [])]
    except Exception:
        pass
    sessions = []
    try:
        from alpaca.trading.requests import GetCalendarRequest
        cal = broker.trading.get_calendar(GetCalendarRequest(
            start=day - timedelta(weeks=GO_LIVE["weeks"]), end=day))
        sessions = [str(c.date)[:10] for c in cal if str(c.date)[:10] <= day.isoformat()]
    except Exception:
        pass
    since = (day - timedelta(weeks=GO_LIVE["weeks"])).isoformat()
    try:
        split = scoreboard.shadow_split(conn, open_pos)
    except Exception:
        split = None
    lines = [f"🗓️ **Saturday CEO report — {day:%a %Y-%m-%d}** (rule freeze until 2027-01-15)", ""]
    lines += golive_lines(closed, equity, sessions, start_times(log_lines),
                          protection_lines(log_lines, since)) + [""]
    lines += orders_lines(orders, day) + [""]
    lines += graded_lines(conn) + [""]
    lines += shadow_report_lines(split) + [""]
    s = credit.status(config)
    lines += ["**Claude credit**", credit.credit_line(s)] + credit.alerts(s)
    return lines


def chunks(lines, limit=1900):
    out, cur = [], ""
    for ln in lines:
        if len(cur) + len(ln) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += ln + "\n"
    if cur.strip():
        out.append(cur)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--no-post", action="store_true")
    a = ap.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv(".env")
    import journal
    day = date.fromisoformat(a.date) if a.date else datetime.now(ET).date()
    with open("bot_config.json", encoding="utf-8") as f:
        config = json.load(f)
    try:
        with open(ORDERS_FILE, encoding="utf-8") as f:
            orders = json.load(f).get("orders", [])
    except (OSError, ValueError):
        orders = []
    try:
        with open(WORKER_LOG, encoding="utf-8", errors="replace") as f:
            log_lines = f.read().splitlines()
    except OSError:
        log_lines = []
    from broker import Broker
    conn = sqlite3.connect(f"file:{journal.DB_FILE}?mode=ro", uri=True)
    try:
        lines = build(day, conn, Broker(), log_lines, orders, config)
    finally:
        conn.close()
    text = "\n".join(lines)
    print(text)
    os.makedirs("reports", exist_ok=True)
    with open(os.path.join("reports", f"saturday_{day.isoformat()}.md"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    if a.no_post:
        return 0
    import alerts
    import trade_alerts
    if trade_alerts.enabled("saturday_report", config):
        for c in chunks(lines):
            alerts.post_async(c)
        alerts.drain()
    return 0


if __name__ == "__main__":
    sys.exit(main())
