"""
trade_table.py — S19. The "Day trades" Discord message and the weekly
summary, as monospace code blocks no wider than 50 characters (one phone
screen). Reporting only.

A trade is a BUY and the SELL that carries its decision_id. The day table
lists every trade opened or closed on the day plus every open position;
result is one of target / lock (a stop that filled at or above entry) /
stop / owner (closed by hand) / time (hard exit date) / open.
"""

import json
import os
from datetime import date, datetime, timedelta

WIDTH = 50
DISCORD_LIMIT = 2000
# The $5,000 paper account took over at 12:47 ET on 2026-10-09 (PM_HANDOFF);
# trades closed earlier that day were on the old account and are not its.
ACCOUNT_START = "2026-10-09 12:47:00"
ACCOUNT_START_EQUITY = 5000.0


# ============================================================ classification

def result_for(reason: str, pnl: float) -> str:
    r = (reason or "").lower()
    if "target" in r:
        return "target"
    if "owner" in r or "manual" in r or r.startswith("exit"):
        return "owner"
    if "hard exit" in r:
        return "time"
    if "stop" in r:
        return "lock" if pnl >= 0 else "stop"
    return "exit"


# ============================================================ data

def load_trades(conn):
    """[{ticker, qty, buy, buy_ts, sell, sell_ts, pnl, reason, target, stop,
    source}] - one per BUY, sell fields None while open."""
    conn.row_factory = __import__("sqlite3").Row
    buys = conn.execute("SELECT * FROM trades WHERE action='BUY' "
                        "ORDER BY id").fetchall()
    sells = conn.execute("SELECT * FROM trades WHERE action='SELL' "
                         "ORDER BY id").fetchall()
    used, out = set(), []
    for b in buys:
        sell = next((s for s in sells if s["id"] not in used
                     and s["id"] > b["id"] and s["ticker"] == b["ticker"]
                     and (b["decision_id"] is None
                          or s["decision_id"] == b["decision_id"])), None)
        if sell is not None:
            used.add(sell["id"])
        ctx = {}
        if b["decision_id"] is not None:
            row = conn.execute("SELECT context FROM decisions WHERE id=?",
                               (b["decision_id"],)).fetchone()
            if row and row["context"]:
                try:
                    ctx = json.loads(row["context"])
                except ValueError:
                    ctx = {}
        out.append({
            "ticker": b["ticker"], "qty": float(b["qty"]),
            "buy": float(b["price"]), "buy_ts": b["timestamp"],
            "sell": float(sell["price"]) if sell else None,
            "sell_ts": sell["timestamp"] if sell else None,
            "pnl": float(sell["pnl_usd"] or 0) if sell else None,
            "reason": sell["reason"] if sell else None,
            "target": ctx.get("target"), "stop": ctx.get("stop"),
        })
    return out


# ============================================================ formatting

def _px(p) -> str:
    if p is None:
        return "-"
    p = float(p)
    if p >= 1000:
        return f"{p:.0f}"
    if p >= 100:
        return f"{p:.1f}"
    return f"{p:.2f}"


def _pnl(x) -> str:
    if x is None:
        return "-"
    a = abs(x)
    body = f"{a:.2f}" if a < 100 else f"{a:.1f}" if a < 1000 else f"{a:.0f}"
    return ("-" if x < 0 else "+") + body


def _money(x) -> str:
    return ("-" if x < 0 else "+") + f"${abs(x):,.2f}"


HEADER = (f"{'TICK':<5} {'QTY':>3} {'BUY':>6} {'SELL':>6} {'TGT':>6} "
          f"{'STOP':>6} {'P&L':>6} RES")


def row_line(ticker, qty, buy, sell, target, stop, pnl, result) -> str:
    sell_cell = "open" if sell is None else _px(sell)
    line = (f"{ticker[:5]:<5} {qty:>3g} {_px(buy):>6} {sell_cell:>6} "
            f"{_px(target):>6} {_px(stop):>6} {_pnl(pnl):>6} {result[:5]}")
    return line[:WIDTH]


def _wrap(words, width=WIDTH):
    """Join summary fragments into lines no wider than `width`."""
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + 2 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur}  {w}" if cur else w
    if cur:
        lines.append(cur)
    return lines


def summary_lines(closed, count=None, open_pnl=None, equity=None):
    """`count` = trades opened or closed in the period (default: closed)."""
    wins = sum(1 for t in closed if t["pnl"] > 0)
    losses = len(closed) - wins
    rate = f"{100 * wins / len(closed):.0f}%" if closed else "-"
    realized = sum(t["pnl"] for t in closed)
    targets = sum(1 for t in closed if result_for(t["reason"], t["pnl"]) == "target")
    locks = sum(1 for t in closed if result_for(t["reason"], t["pnl"]) == "lock")
    words = [f"Trades {len(closed) if count is None else count}",
             f"W {wins}", f"L {losses}", f"Win {rate}",
             f"Realized {_money(realized)}"]
    if open_pnl is not None:
        words.append(f"Open {_money(open_pnl)}")
    if equity is not None:
        words.append(f"Equity ${equity:,.2f}")
    words += [f"Targets {targets}", f"Locks {locks}"]
    return _wrap(words)


def day_table(trades, day: date, open_positions=None, equity=None):
    """Lines of the day table. `open_positions`: {ticker: {qty, price,
    target, stop, unrealized}} from the broker / positions.json."""
    open_positions = open_positions or {}
    d = day.isoformat()
    lines = [HEADER, "-" * len(HEADER)]
    closed_today = []
    count = sum(1 for t in trades if t["buy_ts"].startswith(d)
                or (t["sell_ts"] or "").startswith(d))
    for t in trades:
        if t["sell_ts"] is None:
            continue                      # open: listed from open_positions
        if not (t["buy_ts"].startswith(d) or t["sell_ts"].startswith(d)):
            continue
        if t["sell_ts"].startswith(d):
            closed_today.append(t)
        lines.append(row_line(t["ticker"], t["qty"], t["buy"], t["sell"],
                              t["target"], t["stop"], t["pnl"],
                              result_for(t["reason"], t["pnl"])))
    open_pnl = 0.0
    for tk, p in sorted(open_positions.items()):
        open_pnl += p.get("unrealized") or 0.0
        lines.append(row_line(tk, p["qty"], p.get("entry"), None,
                              p.get("target"), p.get("stop"),
                              p.get("unrealized"), "open"))
    if len(lines) == 2:
        lines.append("(no trades and no open positions)")
    lines.append("-" * len(HEADER))
    lines += summary_lines(closed_today, count=count, open_pnl=open_pnl,
                           equity=equity)
    return [line[:WIDTH] for line in lines]


def period_summary(trades, start: date, end: date, label: str,
                   bought_after: str = None):
    closed = [t for t in trades if t["sell_ts"]
              and start.isoformat() <= t["sell_ts"][:10] <= end.isoformat()
              and (bought_after is None or t["buy_ts"] >= bought_after)]
    return [label] + summary_lines(closed)


def weekly_lines(trades, today: date, equity=None):
    monday = today - timedelta(days=today.weekday())
    lines = period_summary(trades, monday, today,
                           f"Week {monday:%m-%d}..{today:%m-%d}")
    lines.append("")
    lines += period_summary(trades, date.fromisoformat(ACCOUNT_START[:10]),
                            today, f"Since {ACCOUNT_START[:10]} ($5k acct)",
                            bought_after=ACCOUNT_START)
    if equity is not None:
        change = equity - ACCOUNT_START_EQUITY
        lines += _wrap([f"Equity ${equity:,.2f}",
                        f"{_money(change)} "
                        f"({change / ACCOUNT_START_EQUITY * 100:+.2f}%)"])
    return [line[:WIDTH] for line in lines]


# ============================================================ messages

def to_messages(title: str, lines, limit: int = DISCORD_LIMIT):
    """Code-block messages <= limit chars each, split on line boundaries;
    every part repeats the title with (i/n)."""
    def wrap(part, i, n):
        head = f"**{title}**" + (f" ({i}/{n})" if n > 1 else "")
        return f"{head}\n```\n" + "\n".join(part) + "\n```"
    budget = limit - len(wrap([], 9, 9)) - 2
    parts, cur, size = [], [], 0
    for line in lines:
        if cur and size + len(line) + 1 > budget:
            parts.append(cur)
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur or not parts:
        parts.append(cur)
    return [wrap(p, i, len(parts)) for i, p in enumerate(parts, 1)]


# ============================================================ live inputs

def live_open_positions(broker, positions_file="positions.json"):
    """{ticker: {qty, entry, target, stop, unrealized}} for every
    position the broker holds, with target/stop from positions.json."""
    try:
        with open(positions_file, encoding="utf-8") as f:
            local = json.load(f)
    except (OSError, ValueError):
        local = {}
    out = {}
    for p in broker.get_positions():
        s = local.get(p.symbol) or {}
        out[p.symbol] = {
            "qty": abs(float(p.qty)), "entry": float(p.avg_entry_price),
            "target": s.get("profit_target_price"),
            "stop": s.get("trailing_stop_price"),
            "unrealized": float(getattr(p, "unrealized_pl", 0) or 0),
        }
    return out


def build_messages(conn, day: date, broker=None, weekly=None):
    trades = load_trades(conn)
    open_positions, equity = {}, None
    if broker is not None:
        try:
            open_positions = live_open_positions(broker)
            equity = float(broker.get_equity())
        except Exception:
            open_positions, equity = {}, None
    msgs = to_messages(f"Day trades — {day:%a %Y-%m-%d}",
                       day_table(trades, day, open_positions, equity))
    if weekly if weekly is not None else day.weekday() == 4:
        msgs += to_messages(f"Week — {day:%Y-%m-%d}",
                            weekly_lines(trades, day, equity))
    return msgs


def post_messages(messages, url=None):
    """Send each message; return the HTTP statuses. Errors are returned, not
    raised - the drop itself must never fail because Discord did."""
    from discord_webhook import DiscordWebhook
    url = (url or os.getenv("DISCORD_WEBHOOK_URL", "")).strip()
    statuses = []
    for m in messages:
        try:
            resp = DiscordWebhook(url=url, content=m).execute()
            statuses.append(getattr(resp, "status_code", None))
        except Exception as e:
            statuses.append(f"error: {e}")
    return statuses
