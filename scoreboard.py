"""
scoreboard.py — CEO 2026-10-21: the weekly live-vs-owner-ladder scoreboard.

    python scoreboard.py [--date YYYY-MM-DD] [--no-post]

Fridays 16:30 ET (launchd com.tradingbot.scoreboard). For every DAILY trade
on the $5,000 account (bought since 2026-10-09), closed or open: the P&L it
made (or is making) under the LIVE rule - the R/ATR ladder - against what
the owner's 0.5% ladder would have made on the same trade, replayed on
5-minute bars (lock_replay: BE at +0.5%, then 0.5% under the highest close
in 0.25% steps; same entry, original stop and live target; a bar opening
through a level fills at its open; a bar spanning stop and target hits the
stop first). Per trade and total, in dollars.

Live: a closed trade's journaled P&L; an open trade's unrealized P&L at the
broker. Owner ladder: its exit if the replay closed the trade, else the
last bar's close.

The switch rule (agenda 25): switch to the owner ladder if it beats live by
more than $0 in total over at least 10 CLOSED trades. The scoreboard reports
whether the rule is met; switching is a config change the CEO makes.
"""

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime

import pytz

import trade_table

ET = pytz.timezone("America/New_York")
MIN_CLOSED = 10
TITLE = "📊 Weekly scoreboard — live R/ATR vs owner 0.5% ladder"


def owner_params(config):
    return (((config or {}).get("risk_profiles") or {}).get("Moderate") or {}).get("daily_lock")


def daily_trades(conn, config):
    """Account trades (bought since ACCOUNT_START) on daily setups."""
    out = []
    for t in trade_table.load_trades(conn):
        if t["buy_ts"] < trade_table.ACCOUNT_START:
            continue
        if str(t.get("setup") or "").upper().startswith("INTERN"):
            continue
        if not trade_table.is_daily_setup(t.get("setup"), config):
            continue
        out.append(t)
    return out


def score(trades, open_positions, bars_for, params, positions=None):
    """Rows {ticker, buy_ts, status, live, owner, diff, owner_open} and totals."""
    import lock_replay
    policy = lock_replay.shadow_ladder_policy(params)
    positions = positions or {}
    rows = []
    for t in trades:
        closed = t["sell"] is not None and t["reason"] != "partial"
        state = positions.get(t["ticker"]) or {}
        stop = t.get("orig_stop") or state.get("original_stop") or state.get("initial_stop")
        target = (t.get("target") if closed
                  else state.get("profit_target_price") or t.get("target"))
        if closed:
            live = t["pnl"]
        else:
            live = (open_positions.get(t["ticker"]) or {}).get("unrealized")
        owner, owner_open = None, None
        if stop is not None:
            try:
                bars = bars_for(t["ticker"], t["buy_ts"])
            except Exception:
                bars = None
            if bars is not None and not getattr(bars, "empty", True):
                px, ts, _ = lock_replay.replay(bars, t["buy_ts"], t["buy"],
                                               float(stop),
                                               float(target) if target else None,
                                               policy)
                if px is not None:
                    owner = (px - t["buy"]) * t["qty"]
                    owner_open = ts is None
        rows.append({"ticker": t["ticker"], "buy_ts": t["buy_ts"],
                     "status": "closed" if closed else "open",
                     "live": live, "owner": owner, "owner_open": owner_open,
                     "diff": (owner - live) if owner is not None and live is not None else None})
    both = [r for r in rows if r["diff"] is not None]
    closed_rows = [r for r in both if r["status"] == "closed"]
    totals = {
        "live": sum(r["live"] for r in both), "owner": sum(r["owner"] for r in both),
        "closed_n": len(closed_rows),
        "closed_live": sum(r["live"] for r in closed_rows),
        "closed_owner": sum(r["owner"] for r in closed_rows),
        "unscored": [r["ticker"] for r in rows if r["diff"] is None],
    }
    totals["closed_diff"] = totals["closed_owner"] - totals["closed_live"]
    totals["rule_met"] = (totals["closed_n"] >= MIN_CLOSED and totals["closed_diff"] > 0)
    return rows, totals


def _m(x):
    return "-" if x is None else ("-" if x < 0 else "+") + f"{abs(x):,.2f}"


def table_lines(rows, totals, day):
    w = trade_table.WIDTH
    head = f"{'TICK':<5} {'IN':<5} {'ST':<4} {'LIVE $':>9} {'OWNER $':>9} {'DIFF':>8}"
    lines = [head, "-" * w]
    for r in rows:
        mark = "*" if r["owner_open"] else ""
        lines.append(f"{r['ticker'][:5]:<5} {r['buy_ts'][5:10]:<5} "
                     f"{r['status'][:4]:<4} {_m(r['live']):>9} "
                     f"{(_m(r['owner']) + mark):>9} {_m(r['diff']):>8}"[:w])
    if not rows:
        lines.append("(no daily trades on the account yet)")
    lines.append("-" * w)
    lines.append(f"{'TOTAL':<16} {_m(totals['live']):>9} {_m(totals['owner']):>9} "
                 f"{_m(totals['owner'] - totals['live']):>8}"[:w])
    lines.append(f"Closed trades: {totals['closed_n']}  owner - live "
                 f"{_m(totals['closed_diff'])}")
    if totals["rule_met"]:
        lines.append(f"RULE MET: owner ladder ahead over >= {MIN_CLOSED}")
        lines.append("closed trades - switch is a CEO config change.")
    else:
        need = max(0, MIN_CLOSED - totals["closed_n"])
        lines.append(f"Rule: switch if owner > live over >= {MIN_CLOSED} closed")
        lines.append("trades. Not met" + (f" (needs {need} more closed)." if need
                                          else " (owner not ahead)."))
    if any(r["owner_open"] for r in rows):
        lines.append("* owner ladder still holding: valued at last close")
    if totals["unscored"]:
        lines.append("No replay for: " + ", ".join(totals["unscored"]))
    return [ln[:w] for ln in lines]


def build(day, conn, broker, config, positions):
    trades = daily_trades(conn, config)
    open_positions = trade_table.live_open_positions(broker) if broker else {}

    def bars_for(ticker, since):
        days = (day - date.fromisoformat(since[:10])).days + 3
        return broker.get_bars([ticker], timeframe_minutes=5,
                               lookback_days=max(days, 3)).get(ticker)
    return score(trades, open_positions, bars_for, owner_params(config), positions)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="weekly live vs owner-ladder scoreboard")
    ap.add_argument("--date")
    ap.add_argument("--no-post", action="store_true")
    a = ap.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv()
    import journal
    day = date.fromisoformat(a.date) if a.date else datetime.now(ET).date()
    with open("bot_config.json", encoding="utf-8") as f:
        config = json.load(f)
    try:
        with open("positions.json", encoding="utf-8") as f:
            positions = json.load(f)
    except (OSError, ValueError):
        positions = {}
    from broker import Broker
    conn = sqlite3.connect(f"file:{journal.DB_FILE}?mode=ro", uri=True)
    try:
        rows, totals = build(day, conn, Broker(), config, positions)
    finally:
        conn.close()
    lines = table_lines(rows, totals, day)
    print("\n".join(lines))
    if a.no_post:
        return 0
    import alerts
    import trade_alerts
    if not trade_alerts.enabled("scoreboard"):
        return 0
    for m in trade_table.to_messages(f"{TITLE} — {day:%a %Y-%m-%d}", lines):
        alerts.post_async(m)
    alerts.drain()
    return 0


if __name__ == "__main__":
    sys.exit(main())
