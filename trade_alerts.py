"""
trade_alerts.py — S19. One Discord embed per fill. Reporting only.

  BUY  (green): qty, fill, time (ET + Nepal), target, stop (% from entry),
       setup, conviction, $ at risk.
  SELL (green win / red loss / grey breakeven): qty, fill, time, P&L $ and
       %, R, exit reason,
       holding time, % of the way from entry to target.

  UP   (green, amber if a pre-flight failed): the worker finished starting -
       time, PID, capital, open positions, universe, pre-flight results.

Nothing here blocks a caller: every message is built and sent on a
background thread by alerts.post_async (S19b), and every failure is
caught and logged. Alerts are on unless
bot_config.json sets "discord": {"trade_alerts": false} (fills) or
{"startup": false} (the UP message).
"""

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
NEPAL = ZoneInfo("Asia/Kathmandu")
GREEN, RED, GREY, AMBER = 0x2ECC71, 0xE74C3C, 0x95A5A6, 0xF39C12
CONFIG_FILE = "bot_config.json"


# ============================================================ config

def enabled(key: str = "trade_alerts", config: dict = None) -> bool:
    if config is None:
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                config = json.load(f)
        except (OSError, ValueError):
            config = {}
    return bool(((config or {}).get("discord") or {}).get(key, True))


# ============================================================ formatting

def two_zone(ts: datetime) -> str:
    ts = ts if ts.tzinfo else ts.replace(tzinfo=ET)
    return (f"{ts.astimezone(ET):%Y-%m-%d %H:%M} ET / "
            f"{ts.astimezone(NEPAL):%H:%M} Nepal")


def _price(x: float) -> str:
    """A fill as it happened: 2 decimals at least, up to 4 (202.815 stays
    202.815 - :.2f would round the float down to 202.81)."""
    text = f"{x:,.4f}".rstrip("0")
    whole, _, frac = text.partition(".")
    return f"${whole}.{frac.ljust(2, '0')}"


def _money(x: float) -> str:
    return f"{'-' if x < 0 else '+'}${abs(x):,.2f}"


def holding_time(start: datetime, end: datetime) -> str:
    secs = max(0, (end - start).total_seconds())
    days, rem = divmod(int(secs), 86400)
    hours, rem = divmod(rem, 3600)
    return f"{days}d {hours}h" if days else f"{hours}h {rem // 60}m"


def buy_embed(ticker, qty, fill, ts, target=None, stop=None, setup=None,
              conviction=None) -> dict:
    fields = [("Qty", f"{qty:g}"), ("Fill", _price(fill)),
              ("Time", two_zone(ts))]
    if target is not None:
        fields.append(("Target", f"${target:,.2f} "
                                 f"({(target / fill - 1) * 100:+.1f}%)"))
    if stop is not None:
        fields.append(("Stop", f"${stop:,.2f} ({(stop / fill - 1) * 100:+.1f}%)"))
    fields.append(("Setup", setup or "—"))
    fields.append(("Conviction", f"{conviction:g}" if conviction is not None
                   else "rules-only"))
    if stop is not None and stop < fill:
        fields.append(("$ at risk", f"${(fill - stop) * qty:,.2f}"))
    return {"title": f"🟢 BUY {ticker}", "color": GREEN, "fields": fields}


def sell_embed(ticker, qty, fill, ts, entry, reason, entry_ts=None,
               original_stop=None, target=None) -> dict:
    pnl = (fill - entry) * qty
    pct = (fill / entry - 1) * 100 if entry else 0.0
    flat = abs(pnl) < 0.005          # prints as $0.00: a breakeven lock
    win = pnl > 0 and not flat
    fields = [("Qty", f"{qty:g}"), ("Fill", _price(fill)),
              ("Time", two_zone(ts)),
              ("P&L", f"{_money(pnl)} ({pct:+.2f}%)")]
    if original_stop is not None and entry > original_stop:
        fields.append(("R", f"{(fill - entry) / (entry - original_stop):+.2f}R"))
    fields.append(("Exit", reason))
    if entry_ts is not None:
        fields.append(("Held", holding_time(entry_ts, ts)))
    if target is not None and target > entry:
        fields.append(("Toward target",
                       f"{(fill - entry) / (target - entry) * 100:.0f}% of the way"))
    icon, color = (("⚪", GREY) if flat else ("✅", GREEN) if win
                   else ("🔴", RED))
    return {"title": f"{icon} SELL {ticker} — {reason}",
            "color": color, "fields": fields}


def describe_exit(sell_trade_id: int, state: dict = None) -> dict:
    """A SELL embed from the journal: the SELL row, its BUY (same
    decision_id), and the BUY's gatekeeper context for stop and target.
    `state` (the position record) wins where it knows better: the original
    stop and the live target."""
    import journal
    state = state or {}
    with journal._lock, journal._connect() as conn:
        sell = conn.execute("SELECT * FROM trades WHERE id=?",
                            (sell_trade_id,)).fetchone()
        buy = conn.execute(
            "SELECT * FROM trades WHERE action='BUY' AND decision_id=? "
            "AND id < ? ORDER BY id DESC LIMIT 1",
            (sell["decision_id"], sell_trade_id)).fetchone() \
            if sell["decision_id"] is not None else None
        ctx = {}
        if sell["decision_id"] is not None:
            row = conn.execute("SELECT context FROM decisions WHERE id=?",
                               (sell["decision_id"],)).fetchone()
            if row and row["context"]:
                try:
                    ctx = json.loads(row["context"])
                except ValueError:
                    ctx = {}
    entry = state.get("entry_price") or (buy["price"] if buy else sell["price"])
    entry_ts = (datetime.fromisoformat(buy["timestamp"]).replace(tzinfo=ET)
                if buy else None)
    original = (state.get("original_stop") or state.get("initial_stop")
                or ctx.get("stop"))
    target = state.get("profit_target_price") or ctx.get("target")
    ts = datetime.fromisoformat(sell["timestamp"]).replace(tzinfo=ET)
    return sell_embed(sell["ticker"], float(sell["qty"]), float(sell["price"]),
                      ts, float(entry), sell["reason"], entry_ts,
                      float(original) if original is not None else None,
                      float(target) if target is not None else None)


def startup_embed(ts, pid, equity=None, positions=(), universe_n=None,
                  checks=(), regime=None, cycle=None) -> dict:
    """The worker is up. `checks` is [(name, ok, detail)]; any failure turns
    the message amber and lists what failed, so a start that is up but
    degraded never reads as all-clear."""
    failed = [(n, d) for n, ok, d in checks if not ok]
    fields = [("Time", two_zone(ts)), ("PID", str(pid))]
    if cycle is not None:
        fields.append(("First cycle", f"#{cycle} completed"))
    if regime:
        fields.append(("Regime", regime))
    if equity is not None:
        fields.append(("Capital", f"${equity:,.2f}"))
    fields.append(("Open positions",
                   ", ".join(positions) if positions else "none"))
    if universe_n is not None:
        fields.append(("Universe", f"{universe_n} names"))
    for name, ok, detail in checks:
        fields.append((name, ("✅ " if ok else "⚠️ ") + (detail or
                                                        ("ok" if ok else "failed"))))
    title = ("🟢 Desk up" if not failed
             else f"🟠 Desk up — {len(failed)} pre-flight problem(s)")
    return {"title": title, "color": AMBER if failed else GREEN,
            "fields": fields}


# ============================================================ sending
# S19b: every send goes through alerts.post_async. The message is BUILT and
# sent on a background thread - a journal read, the config read, the HTTP
# call - and no caller ever waits (there is no `wait=` any more).

def _send(embed: dict, content: str = None, url: str = None):
    import alerts
    return alerts._send(dict(embed, content=content) if content else embed,
                        url)


def post(embed, config: dict = None, sender=None, key: str = "trade_alerts"):
    """Fire-and-forget. `embed` is a dict or a callable that builds one.
    Returns the thread (tests may join it); never raises, never blocks."""
    import alerts
    send = sender or _send

    def build():
        if not enabled(key, config):
            return None
        return embed() if callable(embed) else embed

    return alerts.post_async(build, sender=lambda payload, url: send(payload))


def alert_buy(**kwargs):
    """Entry fill. Never raises, never blocks."""
    return post(lambda: buy_embed(**kwargs))


def alert_exit(sell_trade_id, state=None):
    """A journaled exit. The journal read (describe_exit) runs on the
    background thread, not on the exit path. Never raises, never blocks."""
    if sell_trade_id is None:
        return None
    snapshot = dict(state or {})            # the caller may reuse its dict
    return post(lambda: describe_exit(sell_trade_id, snapshot))


def alert_startup(**kwargs):
    """"Desk up", from the worker loop after its first completed cycle.
    Never raises, never blocks."""
    return post(lambda: startup_embed(**kwargs), key="startup")
