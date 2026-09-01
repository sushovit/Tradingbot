"""
floor.py — live-floor status dump. One command, paste-ready markdown.

    python floor.py                       -> print to stdout
    python floor.py --to-file             -> also write reports/floor_<stamp>.md
    python floor.py --to-file --discord   -> also post to DISCORD_WEBHOOK_URL

Sections: worker heartbeat age, last 3 cycle summaries, today's detector
fires with filter outcomes, gatekeeper calls (verdict/conviction/shadow
agreement), open positions with % distance to stop and target, and today's
journal counts by source.

Discord delivery is COPYABLE, not just readable: short reports post as one
code-block message; long reports post a headline + the .md attached, plus a
trimmed code block of the two densest sections (detector fires, gatekeeper
calls) for mobile copy. One webhook post per run; webhook failure keeps the
file, logs, and exits 0.

Reads journal.db + bot_status.log + positions.json + the broker.
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

def heartbeat_section(stats: dict) -> list:
    lines = ["## Worker heartbeat"]
    running = os.path.exists(LOCK_FILE)
    stats["running"] = running
    stats["heartbeat_age"] = None
    if not os.path.exists(STATUS_FILE):
        lines.append(f"- Lock file: {'present' if running else 'absent'} — "
                     "no status log yet.")
        return lines
    age = int(datetime.now().timestamp() - os.path.getmtime(STATUS_FILE))
    stats["heartbeat_age"] = age
    state = "🟢 running" if running else "⚪ not running (no lock file)"
    note = ""
    if running and age > 90:
        note = " — ⚠️ STALE (no heartbeat in >90s; worker may be hung)"
    lines.append(f"- {state} | last status write **{age}s ago**{note}")
    return lines


def cycles_section() -> list:
    lines = ["\n## Last cycle summaries"]
    corrupted = False
    try:
        import safe_io
        corrupted = safe_io.is_corrupted(STATUS_FILE)
        entries = safe_io.read_text_tolerant(STATUS_FILE).splitlines()
    except (FileNotFoundError, OSError):
        entries = []
    if corrupted:
        lines.append("_⚠️ Status file contains NUL bytes — a write was "
                     "interrupted (machine crash). Showing what survived._")
    if not entries:
        lines.append("_No status history._")
        return lines
    for entry in entries[:3]:
        # keep it readable: truncate monster ticker lists
        lines.append(f"- `{entry[:400]}`")
    return lines


# ---------------------------------------------------------------- journal

def detector_fires_section(conn, today, stats: dict) -> list:
    lines = ["\n## Detector fires today (deterministic filter outcomes)"]
    stats["fires"] = 0
    if conn is None:
        lines.append("_journal.db not found._")
        return lines
    rows = conn.execute(
        "SELECT timestamp, ticker, setup_name, "
        "json_extract(verdict,'$.rejection_reason') AS filter, "
        "json_extract(context,'$.details') AS details "
        "FROM decisions WHERE source='rules' AND timestamp LIKE ? "
        "ORDER BY id", (f"{today}%",)).fetchall()
    stats["fires"] = len(rows)
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


def gatekeeper_section(conn, today, stats: dict) -> list:
    lines = ["\n## Gatekeeper calls today"]
    stats["gatekeeper"] = 0
    if conn is None:
        lines.append("_journal.db not found._")
        return lines
    rows = conn.execute(
        "SELECT timestamp, ticker, setup_name, source, approved, "
        "conviction_score, agreement, "
        "json_extract(verdict,'$.rejection_reason') AS reason, "
        "json_extract(verdict,'$.reasoning') AS reasoning, "
        "json_extract(verdict,'$.tag') AS tag, "
        "json_extract(verdict,'$.error') AS error "
        "FROM decisions WHERE source IN "
        "('claude','local','local_shadow','smoke_test','ceo') "
        "AND timestamp LIKE ? ORDER BY id", (f"{today}%",)).fetchall()
    stats["gatekeeper"] = len(rows)
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
        elif r["tag"]:
            # Tag rows (e.g. Rule #5 chop_reclaim) carry approved=1 so they
            # stay out of the rejection log — they are NOT gatekeeper
            # approvals and must not read as one (F, 2026-08-17).
            verdict = f"🏷 tag:{r['tag']}"
        else:
            verdict = "✅ approved" if r["approved"] else "❌ rejected"
        agreement = ("—" if r["agreement"] is None
                     else ("agree" if r["agreement"] else "DISAGREE"))
        # An APPROVAL has no rejection_reason, so the old note column was
        # always blank for approvals — the reasoning was journaled but never
        # rendered, which made every approval look unexplained. Show the
        # reasoning for approvals; the rejection reason for rejections.
        note = (r["error"] or r["reason"] or r["reasoning"] or "")
        note = note.replace("|", "/").replace(chr(10), " ")[:110]
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

def positions_section(stats: dict) -> list:
    lines = ["\n## Open positions (distance to stop / target)"]
    stats["positions"] = 0
    try:
        from broker import Broker, BrokerError
        broker = Broker()
        broker_positions = broker.get_positions()
    except Exception as e:
        lines.append(f"_Broker unreachable: {e}_")
        return lines

    stats["positions"] = len(broker_positions)
    if not broker_positions:
        lines.append("_None._")
        return lines

    try:
        with open(POSITIONS_FILE, "r") as f:
            local = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        local = {}

    try:
        import journal as _j
        b_tickers = set(_j.open_b_tickers())
    except Exception:
        b_tickers = set()

    # Stops/targets come from LIVE open orders — cached state goes stale the
    # moment a stop is replaced (floor showed $59.60 while the broker held
    # $61.78 on 2026-08-07).
    live_stops, live_targets = {}, {}
    try:
        for o in broker.get_live_orders():
            otype = str(getattr(o, "order_type", None)
                        or getattr(o, "type", "")).lower()
            if "stop" in otype and getattr(o, "stop_price", None):
                live_stops[o.symbol] = float(o.stop_price)
            elif "limit" in otype and getattr(o, "limit_price", None):
                live_targets[o.symbol] = float(o.limit_price)
    except Exception as e:
        lines.append(f"_Live order read failed ({e}); showing cached levels._")

    lines.append("| Ticker | Tier | Qty | Entry | Current | Unreal. PnL | → Stop | → Target |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for p in broker_positions:
        current = float(getattr(p, "current_price", 0) or 0)
        state = local.get(p.symbol, {})
        tier = "B" if p.symbol in b_tickers else state.get("tier", "A")
        stop = live_stops.get(p.symbol, state.get("trailing_stop_price"))
        target = live_targets.get(p.symbol, state.get("profit_target_price"))
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
        lines.append(f"| {p.symbol} | {tier} | {p.qty} "
                     f"| ${float(p.avg_entry_price):,.2f} "
                     f"| ${current:,.2f} | ${upnl:+,.2f} ({upct:+.2f}%) "
                     f"| {to_stop} | {to_target} |")
    return lines


def probation_section() -> list:
    """Live trade count for every setup still serving probation. Kept on the
    floor view because probation is a live-risk fact, not a report footnote:
    it says these entries are running at half size right now."""
    try:
        import json as _json
        import risk as _risk
        import journal as _journal
        with open("bot_config.json", encoding="utf-8") as f:
            cfg = _json.load(f)
        setups = _risk.probation_setups(cfg)
        if not setups:
            return []
        limit = _risk.probation_limit(cfg)
        lines = ["", "## Setup probation"]
        for name in setups:
            n = _journal.live_entry_count(name)
            mark = "⏳" if _risk.on_probation(name, n, cfg) else "✅"
            lines.append(f"- {mark} {name}: {n}/{limit} probation")
        return lines
    except Exception as e:
        return ["", "## Setup probation", f"_Unavailable: {e}_"]


def build_report():
    """Returns (markdown_text, stats, dense_lines) — dense_lines are the two
    most information-dense sections for the mobile trim-post."""
    import clockline
    today = _today_et()
    stats = {}
    conn = _conn()
    out = ["# Floor status", clockline.two_zone_line()]
    out += heartbeat_section(stats)
    out += cycles_section()
    fires_lines = detector_fires_section(conn, today, stats)
    gk_lines = gatekeeper_section(conn, today, stats)
    out += fires_lines
    out += gk_lines
    out += positions_section(stats)
    out += probation_section()
    out += counts_section(conn, today)
    if conn is not None:
        conn.close()
    return "\n".join(out), stats, fires_lines + gk_lines


DISCORD_MSG_LIMIT = 2000


def _fit_snippet(lines, budget: int) -> str:
    """Trim table rows (oldest first) until the snippet fits the budget.
    Headers and separators survive; the most recent rows are kept."""
    lines = [ln for ln in lines if ln.strip()]

    def is_row(ln):
        return (ln.startswith("| ") and "---" not in ln
                and not ln.startswith("| Time"))

    while len("\n".join(lines)) > budget:
        for i, ln in enumerate(lines):
            if is_row(ln):
                del lines[i]
                break
        else:
            return "\n".join(lines)[:budget]   # nothing left to trim
    return "\n".join(lines)


def post_discord(report: str, stats: dict, dense_lines: list,
                 file_path: str = None):
    """One webhook post per run. Failure keeps the file, logs, never raises."""
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        print("(no webhook configured — skipping Discord post)")
        return
    try:
        from discord_webhook import DiscordWebhook

        fenced = f"```markdown\n{report}\n```"
        if len(fenced) <= DISCORD_MSG_LIMIT:
            # Fits in one message: code block = clean select-copy on any client.
            wh = DiscordWebhook(url=url, content=fenced)
        else:
            hb = stats.get("heartbeat_age")
            state = "🟢" if stats.get("running") else "⚪"
            headline = (f"🏛️ Floor {datetime.now(EASTERN_TZ).strftime('%H:%M ET')} — "
                        f"{state} hb {hb if hb is not None else '?'}s | "
                        f"fires {stats.get('fires', 0)} | "
                        f"gatekeeper {stats.get('gatekeeper', 0)} | "
                        f"open pos {stats.get('positions', 0)}")
            # Mobile copy: trim-post the dense sections under the headline.
            budget = DISCORD_MSG_LIMIT - len(headline) - 20  # fences + newlines
            snippet = _fit_snippet(dense_lines, budget)
            wh = DiscordWebhook(url=url,
                                content=f"{headline}\n```markdown\n{snippet}\n```")
            if file_path and os.path.exists(file_path):
                with open(file_path, "rb") as f:
                    wh.add_file(file=f.read(),
                                filename=os.path.basename(file_path))
        resp = wh.execute()
        print(f"Posted to Discord (HTTP {getattr(resp, 'status_code', '?')})")
    except Exception as e:
        kept = f" — report kept at {file_path}" if file_path else ""
        print(f"(Discord post failed: {e}){kept}")


def main() -> int:
    to_file = "--to-file" in sys.argv
    to_discord = "--discord" in sys.argv

    report, stats, dense_lines = build_report()
    print(report)

    file_path = None
    # --discord needs the file on disk when the report is attachment-sized.
    if to_file or to_discord:
        os.makedirs("reports", exist_ok=True)
        stamp = datetime.now(EASTERN_TZ).strftime("%Y-%m-%d_%H%M")
        file_path = os.path.join("reports", f"floor_{stamp}.md")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"\nWritten: {file_path}")

    if to_discord:
        post_discord(report, stats, dense_lines, file_path)

    return 0   # webhook failure must never fail the scheduled run


if __name__ == "__main__":
    sys.exit(main())
