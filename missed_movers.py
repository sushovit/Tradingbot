"""
missed_movers.py — S33. The day's top universe gainers and why the desk did
not trade them. Reporting only; ZERO API calls (no Alpaca, no Anthropic, no
network of any kind except the one Discord post).

    python missed_movers.py                     # today (ET), post + file
    python missed_movers.py --date 2026-10-16   # another day
    python missed_movers.py --no-post           # file + stdout only
    python missed_movers.py --root DIR --out DIR   # read DIR's data, write
                                                   # the report elsewhere

Scheduled by jobs/macos/com.tradingbot.missed_movers.plist, Mon-Fri 16:20
ET. Writes reports/missed_movers_<YYYY-MM-DD>.md and posts a phone-width
table to Discord through alerts.post_async, unless bot_config.json sets
"discord": {"missed_movers": false}.

WHERE THE DAY'S % CHANGE COMES FROM (local files only)
------------------------------------------------------
1. universe_today.json, when its "date" is the report date. It is the
   worker's universe itself (every qualified name the detectors scan), and
   each candidate carries change_pct: Alpaca's movers percent_change, or
   last trade / prior completed close, computed by universe.refresh at its
   last run. That refresh runs at worker start and in the 10:00 and 15:00
   snapshots, so at 16:20 the figures are the ~15:00 ET move, NOT the
   16:00 close. The report states the timestamp.
2. Otherwise (a past --date: universe_today.json is overwritten every
   refresh) the "# Universe" table of that day's latest
   reports/scan_ET<date>_<HHMM>.md, or drop/session_ET<date>_<HHMM>.md.
   Those list only the TOP 20 BY SCORE (dollar volume), not the whole
   universe - the report says "partial".
A ticker whose % change is not in the chosen source is skipped; nothing is
ever fetched to fill a gap. No source for the date -> the report says so.

WHY IT WAS NOT TRADED
---------------------
Stage vocabulary is study_trade_funnel.STAGES (S21): the pipeline order of
streamlit_app._worker_loop, and each journaled reason key's stage
("rules filter" for adx_low / volume_low / ..., "gatekeeper", "size" ...).
Per ticker, from the day's journal (read-only):
  traded       - a BUY on the day (trades table)
  in position  - held from an earlier BUY (detection is skipped while held)
  <stage>      - the FURTHEST stage any of the day's worker decision rows
                 (source rules / claude) reached; the reason is that row's
                 key (rules: the filter key; gatekeeper: outcomes.reason_key
                 bucket + conviction; an approval at or above the conviction
                 threshold with no fill is "approved, no fill row").
  from the last bot_status.log cycle of the day, when the journal has
  nothing: "Waiting (No Signal)" -> no signal, "In Pos" -> in position,
  "No Data" -> no data, "Pass (...)" / "Gatekeeper blocked (...)" -> their
  stage; a ticker missing from that cycle -> not monitored.
  no signal    - no journal row and no cycle status: no detector fired.
bot_status.log keeps only the latest cycle, so cycle status exists only for
the day the job runs on (not for a past --date).
"""

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import outcomes
import study_trade_funnel as funnel
import trade_table

ET = ZoneInfo("America/New_York")
TOP_N = 10
WIDTH = trade_table.WIDTH
CONFIG_KEY = "missed_movers"
UNIVERSE_FILE = "universe_today.json"
STATUS_FILE = "bot_status.log"
CONFIG_FILE = "bot_config.json"
WORKER_SOURCES = ("rules", "claude")

# Display labels (<= 13 chars) for the phone table; the .md uses full names.
SHORT_STAGE = {
    "rules filter": "rules filter",
    "circuit breaker": "circuit brkr",
    "regime (SPY chop)": "regime",
    "entry window": "entry window",
    "max positions / check_signal": "max pos/check",
    "gatekeeper": "gatekeeper",
    "approved, no fill row": "approved",
    "probation": "probation",
    "size": "size",
    "duplicate": "duplicate",
    "filled": "traded",
    "traded": "traded",
    "in position": "in position",
    "no signal": "no signal",
    "no data": "no data",
    "not monitored": "not monitored",
    "order failed": "order failed",
}


# ============================================================ % change source

def _from_universe_file(path, day):
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return None
    if payload.get("date") != day:
        return None
    moves = {}
    for c in payload.get("candidates") or []:
        sym, chg = c.get("symbol"), c.get("change_pct")
        if sym and isinstance(chg, (int, float)):
            moves[sym] = float(chg)
    if not moves:
        return None
    stamp = str(payload.get("generated_at") or "")[11:16] or "?"
    return _source(moves, os.path.basename(path), stamp,
                   f"all {len(moves)} universe names", partial=False)


def _source(moves, name, at, scope, partial):
    return {"moves": moves, "partial": partial, "file": name, "at": at,
            "scope": scope, "label": f"{name} @ {at} ET, {scope}"}


_ROW = re.compile(r"^\|\s*\d+\s*\|\s*([A-Z][A-Z0-9.\-]*)\s*\|[^|]*\|[^|]*\|"
                  r"\s*([+-]?\d+(?:\.\d+)?)%\s*\|")


def parse_universe_table(text):
    """{ticker: move %} from the last '# Universe' table in a scan/drop file."""
    start = max(text.rfind("# Universe —"), text.rfind("# Universe -"))
    if start < 0:
        return {}
    moves = {}
    for line in text[start:].splitlines()[1:]:
        if line.startswith("#") or line.startswith("---"):
            break
        m = _ROW.match(line.strip())
        if m:
            moves[m.group(1)] = float(m.group(2))
    return moves


def _from_scan_files(root, day):
    files = (glob.glob(os.path.join(root, "reports", f"scan_ET{day}_*.md"))
             + glob.glob(os.path.join(root, "drop", f"session_ET{day}_*.md")))
    # Latest time of day first; a scan and a drop of the same minute hold
    # the same table.
    files.sort(key=lambda p: os.path.basename(p).rsplit("_", 1)[-1],
               reverse=True)
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                moves = parse_universe_table(f.read())
        except OSError:
            continue
        if moves:
            hhmm = os.path.basename(path).rsplit("_", 1)[-1][:4]
            return _source(moves, os.path.basename(path),
                           f"{hhmm[:2]}:{hhmm[2:]}",
                           f"top {len(moves)} by score only (partial)",
                           partial=True)
    return None


def load_moves(root, day):
    """{"moves": {ticker: pct}, "partial": bool, "label": str} or None."""
    return (_from_universe_file(os.path.join(root, UNIVERSE_FILE), day)
            or _from_scan_files(root, day))


def top_gainers(moves, n=TOP_N):
    """[(ticker, pct)] - the n largest % changes, ties by ticker."""
    return sorted(moves.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


# ============================================================ cycle status

_CYCLE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}):\d{2}\] Cycle #\d+")


def last_cycle(path, day):
    """({ticker: status}, 'HH:MM') from the day's last cycle line, or
    ({}, None)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().replace("\x00", "").splitlines()
    except OSError:
        return {}, None
    best = None
    for line in lines:
        m = _CYCLE.match(line)
        if m and m.group(1) == day and (best is None or line[:21] > best[:21]):
            best = line
    if best is None:
        return {}, None
    body = best.split("): ", 1)[1] if "): " in best else ""
    out = {}
    for part in body.split(" | "):
        tk, sep, status = part.partition(": ")
        if sep and re.fullmatch(r"[A-Z][A-Z0-9.\-]*", tk.strip()):
            out[tk.strip()] = status.strip()
    return out, _CYCLE.match(best).group(2)


def stage_from_status(status):
    """(stage, reason) for one bot_status cycle entry."""
    s = (status or "").strip()
    if s.startswith("Waiting (No Signal)"):
        return "no signal", "no detector fired"
    if s.startswith("In Pos"):
        return "in position", s
    if s.startswith("No Data"):
        return "no data", "no bars (last cycle)"
    if s.startswith("Gatekeeper") or s.startswith("Waiting (gatekeeper"):
        return "gatekeeper", s
    if s.startswith("chop_reclaim"):
        return "regime (SPY chop)", "chop_reclaim exemption"
    if s.startswith("duplicate"):
        return "duplicate", "duplicate_entry_blocked"
    if s.startswith("Order failed"):
        return "order failed", s
    m = re.match(r"Pass \((.*)\)$", s)
    if m:
        inner = m.group(1)
        fixed = {"circuit breaker": "circuit_breaker",
                 "SPY bearish": "spy_bearish",
                 "outside hours": "outside_hours",
                 "max positions": "max_positions"}
        if inner in fixed:
            key = fixed[inner]
        elif ": " in inner:
            head, _, tail = inner.partition(": ")
            # "size_zero: stop $x vs budget $y" (a reason key first) or
            # "setup: filter" (a detector rejection).
            key = head if head in funnel.REASON_STAGE else tail.split()[0]
        else:
            key = inner
        return funnel.REASON_STAGE.get(key, "rules filter"), key
    return "other", s


# ============================================================ journal

def _json(text):
    try:
        return json.loads(text or "{}") or {}
    except ValueError:
        return {}


def journal_facts(conn, day, threshold):
    """{ticker: {"stage", "reason", "setup", "rows"}} from the day's worker
    decisions (furthest stage wins; the latest row on a tie), plus
    {ticker: (setup, price)} BUYs on the day and {ticker: buy_date} for
    positions held from earlier."""
    conn.row_factory = sqlite3.Row
    facts = {}
    rows = conn.execute(
        "SELECT id, timestamp, ticker, setup_name, source, approved, "
        "conviction_score, context, verdict FROM decisions "
        "WHERE substr(timestamp,1,10)=? AND source IN (%s) ORDER BY id"
        % ",".join("?" * len(WORKER_SOURCES)),
        (day, *WORKER_SOURCES)).fetchall()
    for r in rows:
        setup = r["setup_name"] or ""
        if setup.upper().startswith("CEO"):
            continue                       # order-sheet rows, not the funnel
        verdict, ctx = _json(r["verdict"]), _json(r["context"])
        if r["source"] == "claude":
            conv = r["conviction_score"]
            approved = bool(r["approved"]) and "error" not in verdict
            if approved and conv is not None and conv >= threshold:
                stage, reason = "approved, no fill row", f"c{conv:g}"
            else:
                raw = (verdict.get("rejection_reason") or verdict.get("error")
                       or (f"conviction={conv}" if conv is not None else ""))
                key = outcomes.reason_key("claude", raw)
                if "error" in verdict:
                    key = "gatekeeper_error"
                stage = "gatekeeper"
                reason = key.replace("gatekeeper_", "gk_") + (
                    f" c{conv:g}" if conv is not None else "")
                ctx = dict(ctx, details=str(raw))
        else:
            key = verdict.get("rejection_reason")
            if key is None and verdict.get("tag"):
                stage, reason = "regime (SPY chop)", f"{verdict['tag']} (passed)"
            else:
                key = key or "unspecified"
                stage = funnel.REASON_STAGE.get(key, "rules filter")
                reason = key
        rank = funnel.STAGE_RANK.get(stage, 0)
        cur = facts.get(r["ticker"])
        entry = {"stage": stage, "reason": reason, "setup": setup,
                 "details": str(ctx.get("details") or ""), "rank": rank,
                 "rows": (cur["rows"] if cur else 0) + 1}
        if cur is None or rank >= cur["rank"]:
            facts[r["ticker"]] = entry
        else:
            cur["rows"] += 1

    bought, held = {}, {}
    for t in trade_table.load_trades(conn):
        if str(t.get("setup") or "").upper().startswith("INTERN"):
            continue
        buy_day = t["buy_ts"][:10]
        if buy_day == day:
            bought.setdefault(t["ticker"], (t.get("setup") or "", t["buy"]))
        elif buy_day < day and (t["sell_ts"] is None or t["sell_ts"][:10] >= day
                                or t["reason"] == "partial"):
            held.setdefault(t["ticker"], buy_day)
    return facts, bought, held


def worker_ran(conn, day):
    row = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE substr(timestamp,1,10)=? AND "
        "source IN ('rules','claude')", (day,)).fetchone()
    return bool(row and row[0])


# ============================================================ classification

def classify(ticker, facts, bought, held, cycle, cycle_seen):
    """{"stage", "reason", "setup", "details", "status"} for one ticker."""
    status = cycle.get(ticker)
    base = {"setup": "", "details": "", "status": status or "", "rows": 0}
    if ticker in bought:
        setup, price = bought[ticker]
        return dict(base, stage="traded", setup=setup,
                    reason=f"bought @ {trade_table._px(price)}")
    if ticker in held:
        return dict(base, stage="in position",
                    reason=f"held since {held[ticker]}")
    f = facts.get(ticker)
    if f:
        return dict(base, stage=f["stage"], reason=f["reason"],
                    setup=f["setup"], details=f["details"], rows=f["rows"])
    if status:
        stage, reason = stage_from_status(status)
        return dict(base, stage=stage, reason=reason)
    if cycle_seen:
        return dict(base, stage="not monitored",
                    reason="not in the day's last cycle")
    return dict(base, stage="no signal", reason="no detector fired")


def build(day, root=".", db_file=None, config=None):
    """The whole report as a dict. Reads files only."""
    if config is None:
        try:
            with open(os.path.join(root, CONFIG_FILE), encoding="utf-8") as f:
                config = json.load(f)
        except (OSError, ValueError):
            config = {}
    threshold = outcomes.conviction_threshold(config)
    src = load_moves(root, day)
    cycle, cycle_at = last_cycle(os.path.join(root, STATUS_FILE), day)
    db_file = db_file or __import__("journal").DB_FILE
    facts, bought, held, ran = {}, {}, {}, bool(cycle)
    if os.path.exists(db_file):
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        try:
            facts, bought, held = journal_facts(conn, day, threshold)
            ran = ran or worker_ran(conn, day)
        finally:
            conn.close()
    rows = []
    for tk, pct in top_gainers(src["moves"]) if src else []:
        rows.append(dict(classify(tk, facts, bought, held, cycle, bool(cycle)),
                         ticker=tk, pct=pct))
    return {"day": day, "source": src, "rows": rows, "cycle_at": cycle_at,
            "worker_ran": ran}


# ============================================================ formatting

def _pct(x):
    return f"{x:+.1f}"


def table_lines(report):
    """Phone-width (<= 50 chars) lines of the Discord table."""
    src = report["source"]
    if not src:
        return [f"No local % change data for {report['day']}.",
                "(universe_today.json is another day's and no",
                " scan/drop universe table exists.)"]
    lines = [f"% chg: {src['file']} @ {src['at']} ET", f"  {src['scope']}"]
    if report["cycle_at"]:
        lines.append(f"Last cycle {report['cycle_at']} ET")
    head = f"{'#':>2} {'TICK':<5} {'CHG%':>6} {'STAGE':<13} REASON"
    lines += [head, "-" * WIDTH]
    for i, r in enumerate(report["rows"], 1):
        stage = SHORT_STAGE.get(r["stage"], r["stage"])[:13]
        line = (f"{i:>2} {r['ticker'][:5]:<5} {_pct(r['pct']):>6} "
                f"{stage:<13} {r['reason']}")
        lines.append(line[:WIDTH])
    if not report["rows"]:
        lines.append("(no tickers with a % change)")
    lines.append("-" * WIDTH)
    counts = {}
    for r in report["rows"]:
        k = SHORT_STAGE.get(r["stage"], r["stage"])
        counts[k] = counts.get(k, 0) + 1
    lines += trade_table._wrap([f"{k} {v}" for k, v in
                                sorted(counts.items(), key=lambda kv: -kv[1])])
    return [line[:WIDTH] for line in lines]


def _md_cell(text):
    return str(text or "").replace("|", "/").replace("\n", " ").strip() or "—"


def markdown(report):
    day = report["day"]
    src = report["source"]
    out = [f"# Missed movers — {day}", "",
           f"_Generated {datetime.now(ET):%Y-%m-%d %H:%M} ET by "
           f"missed_movers.py (local files only, zero API calls)._", ""]
    if not src:
        out += [f"No local % change data for {day}: universe_today.json is "
                f"for another day and no reports/scan_ET{day}_*.md or "
                f"drop/session_ET{day}_*.md universe table exists.", ""]
        return "\n".join(out)
    out.append(f"- % change source: **{src['label']}**")
    if src["partial"]:
        out.append("- Partial universe: the scan file lists only the top 20 "
                   "names by score, so a bigger gainer outside them is not "
                   "ranked here.")
    out.append(f"- Last bot_status.log cycle on {day}: "
               + (f"{report['cycle_at']} ET" if report["cycle_at"]
                  else "none (status shown only for the day the job runs)"))
    if not report["worker_ran"]:
        out.append(f"- No worker activity journaled on {day} (holiday or "
                   f"worker down).")
    out += ["", f"## Top {TOP_N} gainers by % change", "",
            "| # | Ticker | Change % | Stage | Reason | Setup | Detail | "
            "Last cycle status | Journal rows |",
            "|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(report["rows"], 1):
        out.append(f"| {i} | {r['ticker']} | {_pct(r['pct'])}% | "
                   f"{_md_cell(r['stage'])} | {_md_cell(r['reason'])} | "
                   f"{_md_cell(r['setup'])} | {_md_cell(r['details'][:160])} | "
                   f"{_md_cell(r['status'][:120])} | {r['rows']} |")
    out += ["", "Stages follow study_trade_funnel.STAGES (the worker's "
            "pipeline order); a ticker with several journal rows shows the "
            "furthest stage it reached that day.", ""]
    return "\n".join(out)


def title(day):
    return f"Missed movers — {datetime.strptime(day, '%Y-%m-%d'):%a %Y-%m-%d}"


def messages(report):
    return trade_table.to_messages(title(report["day"]), table_lines(report))


# ============================================================ run

def write_report(report, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"missed_movers_{report['day']}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(markdown(report))
    return path


def post(report):
    """Queue every message on alerts.post_async. Returns how many."""
    import alerts
    msgs = messages(report)
    for m in msgs:
        alerts.post_async(m)
    return len(msgs)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--date", help="YYYY-MM-DD (default: today ET)")
    ap.add_argument("--no-post", action="store_true",
                    help="write the file and print; do not post to Discord")
    ap.add_argument("--root", default=".",
                    help="folder holding journal.db, universe_today.json, "
                         "bot_status.log, reports/ (default: .)")
    ap.add_argument("--out", default=None,
                    help="report folder (default: <root>/reports)")
    args = ap.parse_args(argv)
    day = args.date or datetime.now(ET).strftime("%Y-%m-%d")
    datetime.strptime(day, "%Y-%m-%d")
    root = args.root
    try:
        with open(os.path.join(root, CONFIG_FILE), encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError):
        config = {}
    db_file = (__import__("journal").DB_FILE if root == "." else
               os.path.join(root, "journal.db"))
    report = build(day, root, db_file=db_file, config=config)
    path = write_report(report, args.out or os.path.join(root, "reports"))
    print(f"Wrote {path}")
    for m in messages(report):
        print(m)
    if args.no_post:
        print("(--no-post: Discord skipped)")
    elif not report["worker_ran"]:
        print(f"(no worker activity on {day}: nothing posted)")
    else:
        import trade_alerts
        if not trade_alerts.enabled(CONFIG_KEY, config):
            print(f"(discord.{CONFIG_KEY} is off in bot_config.json)")
        else:
            from dotenv import load_dotenv
            load_dotenv(os.path.join(root, ".env"))
            print(f"Discord: {post(report)} message(s) queued")
    import alerts
    alerts.drain()
    return 0


if __name__ == "__main__":
    sys.exit(main())
