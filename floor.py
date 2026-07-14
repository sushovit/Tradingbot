"""
floor.py — live-floor status dump. One command, paste-ready markdown.

    python floor.py

Sections: worker heartbeat age, last 3 cycle summaries, today's detector
fires with filter outcomes, gatekeeper calls (verdict/conviction/shadow
agreement), open positions with % distance to stop and target, and today's
journal counts by source.

Strictly read-only: reads journal.db + bot_status.log + positions.json +
the broker. Creates no new state.
"""

import os
import json
import sqlite3
import sys
from datetime import datetime

import pytz
from dotenv import load_dotenv

load_dotenv()

# Windows consoles default to cp1252 — force UTF-8 so emoji/dashes survive.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

EASTERN_TZ = pytz.timezone("US/Eastern")
STATUS_FILE = "bot_status.log"
LOCK_FILE = "bot.run"
POSITIONS_FILE = "positions.json"
JOURNAL_DB = "journal.db"


def _today_et() -> str:
    return datetime.now(EASTERN_TZ).strftime("%Y-%m-%d")


def _conn():
    if not os.path.exists(JOURNAL_DB):
        return None
    conn = sqlite3.connect(f"file:{JOURNAL_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------- heartbeat

def heartbeat_section() -> list:
    lines = ["## Worker heartbeat"]
    running = os.path.exists(LOCK_FILE)
    if not os.path.exists(STATUS_FILE):
        lines.append(f"- Lock file: {'present' if running else 'absent'} — "
                     "no status log yet.")
        return lines
    age = int(datetime.now().timestamp() - os.path.getmtime(STATUS_FILE))
    state = "🟢 running" if running else "⚪ not running (no lock file)"
    note = ""
    if running and age > 90:
        note = " — ⚠️ STALE (no heartbeat in >90s; worker may be hung)"
    lines.append(f"- {state} | last status write **{age}s ago**{note}")
    return lines


def cycles_section() -> list:
    lines = ["\n## Last cycle summaries"]
    try:
        with open(STATUS_FILE, "r", encoding="utf-8", errors="replace") as f:
            entries = [ln for ln in f.read().splitlines() if ln.strip()]
    except FileNotFoundError:
        entries = []
    if not entries:
        lines.append("_No status history._")
        return lines
    for entry in entries[:3]:
        # keep it readable: truncate monster ticker lists
        lines.append(f"- `{entry[:400]}`")
    return lines


# ---------------------------------------------------------------- journal

def detector_fires_section(conn, today) -> list:
    lines = ["\n## Detector fires today (deterministic filter outcomes)"]
    if conn is None:
        lines.append("_journal.db not found._")
        return lines
    rows = conn.execute(
        "SELECT timestamp, ticker, setup_name, "
        "json_extract(verdict,'$.rejection_reason') AS filter, "
        "json_extract(context,'$.details') AS details "
        "FROM decisions WHERE source='rules' AND timestamp LIKE ? "
        "ORDER BY id", (f"{today}%",)).fetchall()
    if not rows:
        lines.append("_None — no detector fired into a filter today._")
        return lines
    lines.append("| Time (ET) | Ticker | Setup | Filter outcome | Details |")
    lines.append("|---|---|---|---|---|")
    for r in rows:
        details = (r["details"] or "").replace("|", "/")[:60]
        lines.append(f"| {r['timestamp'][11:19]} | {r['ticker']} "
                     f"| {r['setup_name']} | {r['filter']} | {details} |")
    return lines


def gatekeeper_section(conn, today) -> list:
    lines = ["\n## Gatekeeper calls today"]
    if conn is None:
        lines.append("_journal.db not found._")
        return lines
    rows = conn.execute(
        "SELECT timestamp, ticker, setup_name, source, approved, "
        "conviction_score, agreement, "
        "json_extract(verdict,'$.rejection_reason') AS reason, "
        "json_extract(verdict,'$.error') AS error "
        "FROM decisions WHERE source IN "
        "('claude','local','local_shadow','smoke_test','ceo') "
        "AND timestamp LIKE ? ORDER BY id", (f"{today}%",)).fetchall()
    if not rows:
        lines.append("_None._")
        return lines
    if len(rows) > 24:
        lines.append(f"_{len(rows)} calls today — showing the most recent 24._")
        rows = rows[-24:]
    lines.append("| Time (ET) | Ticker | Setup | Source | Verdict | Conviction | Agreement | Note |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        if r["error"]:
            verdict = "ERROR"
        else:
            verdict = "✅ approved" if r["approved"] else "❌ rejected"
        agreement = ("—" if r["agreement"] is None
                     else ("agree" if r["agreement"] else "DISAGREE"))
        note = (r["error"] or r["reason"] or "").replace("|", "/")[:50]
        conviction = r["conviction_score"] if r["conviction_score"] is not None else "—"
        lines.append(f"| {r['timestamp'][11:19]} | {r['ticker']} | {r['setup_name']} "
                     f"| {r['source']} | {verdict} | {conviction} | {agreement} | {note} |")
    return lines


def counts_section(conn, today) -> list:
    lines = ["\n## Journal counts today (by source)"]
    if conn is None:
        lines.append("_journal.db not found._")
        return lines
    rows = conn.execute(
        "SELECT source, COUNT(*) AS n FROM decisions WHERE timestamp LIKE ? "
        "GROUP BY source ORDER BY n DESC", (f"{today}%",)).fetchall()
    trades = conn.execute(
        "SELECT action, COUNT(*) AS n FROM trades WHERE timestamp LIKE ? "
        "GROUP BY action", (f"{today}%",)).fetchall()
    if not rows and not trades:
        lines.append("_Empty._")
        return lines
    for r in rows:
        lines.append(f"- decisions/{r['source']}: **{r['n']}**")
    for t in trades:
        lines.append(f"- trades/{t['action']}: **{t['n']}**")
    return lines


# ---------------------------------------------------------------- broker

def positions_section() -> list:
    lines = ["\n## Open positions (distance to stop / target)"]
    try:
        from broker import Broker, BrokerError
        broker = Broker()
        broker_positions = broker.get_positions()
    except Exception as e:
        lines.append(f"_Broker unreachable: {e}_")
        return lines

    if not broker_positions:
        lines.append("_None._")
        return lines

    try:
        with open(POSITIONS_FILE, "r") as f:
            local = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        local = {}

    lines.append("| Ticker | Qty | Entry | Current | Unreal. PnL | → Stop | → Target |")
    lines.append("|---|---|---|---|---|---|---|")
    for p in broker_positions:
        current = float(getattr(p, "current_price", 0) or 0)
        state = local.get(p.symbol, {})
        stop = state.get("trailing_stop_price")
        target = state.get("profit_target_price")
        if current and stop:
            to_stop = f"-{(current - float(stop)) / current * 100:.1f}% (${float(stop):,.2f})"
        else:
            to_stop = "—"
        if current and target:
            to_target = f"+{(float(target) - current) / current * 100:.1f}% (${float(target):,.2f})"
        else:
            to_target = "—"
        upnl = float(getattr(p, "unrealized_pl", 0) or 0)
        upct = float(getattr(p, "unrealized_plpc", 0) or 0) * 100
        lines.append(f"| {p.symbol} | {p.qty} | ${float(p.avg_entry_price):,.2f} "
                     f"| ${current:,.2f} | ${upnl:+,.2f} ({upct:+.2f}%) "
                     f"| {to_stop} | {to_target} |")
    return lines


def main() -> int:
    today = _today_et()
    now = datetime.now(EASTERN_TZ).strftime("%Y-%m-%d %H:%M:%S ET")
    out = [f"# Floor status — {now}"]
    out += heartbeat_section()
    out += cycles_section()
    conn = _conn()
    out += detector_fires_section(conn, today)
    out += gatekeeper_section(conn, today)
    out += positions_section()
    out += counts_section(conn, today)
    if conn is not None:
        conn.close()
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
