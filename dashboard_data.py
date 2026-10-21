"""
dashboard_data.py — the numbers the dashboard shows, computed exactly and
kept out of the page code so they can be tested. Read-only: nothing here
places, cancels or changes anything.

  desk_status()      worker running?, heartbeat age, last cycle, market state
  position_rows()    one exact row per open position: broker fill, live stop
                     at the broker, what that stop locks or risks, target
                     progress, and the next ladder trigger
  status_summary()   the worker's latest status line, parsed into counts
  decision_rows()    today's gatekeeper calls
"""

import json
import math
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import position_mgmt as pm

ET = ZoneInfo("America/New_York")


# ============================================================ worker / desk

def desk_status(lock_file="bot.run", status_file="bot_status.log", now=None):
    now = now or time.time()
    running = os.path.exists(lock_file)
    pid = None
    if running:
        try:
            with open(lock_file, encoding="utf-8") as f:
                pid = int((f.read().split() or ["0"])[0]) or None
        except (OSError, ValueError):
            pid = None
    try:
        age = now - os.path.getmtime(status_file)
    except OSError:
        age = None
    try:
        with open(status_file, encoding="utf-8", errors="replace") as f:
            latest = (f.read().splitlines() or [""])[0]
    except OSError:
        latest = ""
    return {"running": running, "pid": pid, "heartbeat_age": age,
            "latest": latest, **status_summary(latest)}


_STATUS = re.compile(r"^\[(?P<ts>[0-9: -]+)\]\s*(?P<body>.*)$")


def status_summary(line: str) -> dict:
    """Parse one status line: time, cycle number and per-ticker outcome
    counts ("Waiting", "Pass", "In Pos", "Gatekeeper blocked", ...)."""
    out = {"time": None, "cycle": None, "counts": {}, "headline": line or ""}
    m = _STATUS.match(line or "")
    if not m:
        return out
    out["time"] = m.group("ts")
    body = m.group("body")
    cyc = re.search(r"Cycle #(\d+)", body)
    if cyc:
        out["cycle"] = int(cyc.group(1))
    if "|" in body:
        counts = {}
        for part in body.split("|")[1:]:
            # The first ticker shares its segment with the header
            # ("Monitoring Live (Alpaca paper): TXN: In Pos ...").
            if "Monitoring Live" in part and "):" in part:
                part = part.split("):", 1)[1]
            _, _, outcome = part.partition(":")
            outcome = outcome.strip()
            key = ("In position" if outcome.startswith("In Pos")
                   else "Blocked by gatekeeper" if outcome.startswith("Gatekeeper blocked")
                   else "Passed (filter)" if outcome.startswith("Pass")
                   else "Waiting" if outcome.startswith("Waiting")
                   else "Bought" if outcome.startswith("BOUGHT")
                   else "Sold" if outcome.startswith("SOLD")
                   else "No data" if outcome.startswith("No Data")
                   else "Other")
            counts[key] = counts.get(key, 0) + 1
        out["counts"] = counts
        out["headline"] = body.split("|")[0].strip()
    else:
        out["headline"] = body
    return out


# ============================================================ positions

def live_legs(orders) -> dict:
    """{symbol: {"stop": price, "target": price}} from the broker's working
    orders (bracket legs)."""
    legs = {}
    for o in orders or []:
        kind = str(getattr(o, "order_type", None) or getattr(o, "type", "")).lower()
        side = str(getattr(o, "side", "")).lower()
        if "sell" not in side:
            continue
        rec = legs.setdefault(o.symbol, {})
        if "stop" in kind and getattr(o, "stop_price", None) is not None:
            rec["stop"] = float(o.stop_price)
        elif "limit" in kind and getattr(o, "limit_price", None) is not None:
            rec["target"] = float(o.limit_price)
    return legs


def _first_cent(x: float) -> float:
    """The first whole cent at or above a trigger level - the lowest close
    that actually fires it (281.651 -> 281.66, not 281.65)."""
    return math.ceil(x * 100 - 1e-6) / 100


def next_trigger(entry, original_stop, stop, highest_close, risk_profile):
    """Plain-English 'what moves the stop next' for the daily lock in use."""
    ttype = pm.trailing_type_for({"timeframe": "daily"}, risk_profile or {})
    if entry is None:
        return "—"
    if ttype == pm.LADDER:
        p = pm.ladder_params(risk_profile)
        start = _first_cent(entry * (1 + p["start_pct"] / 100))
        if (highest_close or 0) < start:
            return f"Breakeven at a 5-min close ≥ ${start:,.2f}"
        hc = highest_close
        return (f"Trailing: stop follows the high close (now ${hc:,.2f}) "
                f"at −{p['cushion_pct']:g}% → ${hc * (1 - p['cushion_pct'] / 100):,.2f}")
    if ttype == pm.PCT_LOCK:
        p = pm.pct_lock_params(risk_profile)
        for key, label in (("step1_trigger_pct", "Step 1"),
                           ("step2_trigger_pct", "Step 2"),
                           ("trail_trigger_pct", "Trail")):
            level = _first_cent(entry * (1 + p[key] / 100))
            if (highest_close or 0) < level:
                return f"{label} at a 5-min close ≥ ${level:,.2f}"
        return "Trailing the high close"
    if ttype == pm.R_ATR:
        return r_atr_next(entry, original_stop, stop, highest_close, risk_profile)
    return f"Rule: {ttype}"


def r_atr_next(entry, original_stop, stop, highest_close, risk_profile):
    """The live R/ATR ladder's next stop move, in plain English: the next
    step that would actually RAISE the stop (a step that asks for the level
    the stop already has is skipped), or the ATR trail once past them."""
    if original_stop is None or original_stop >= entry:
        return "R/ATR ladder (original stop unknown)"
    p = pm.r_atr_params(risk_profile)
    risk = entry - original_stop
    reached = ((highest_close or entry) - entry) / risk
    current = stop if stop is not None else original_stop
    for trigger_r, stop_r in p["steps"]:
        if reached >= trigger_r:
            continue
        new_stop = entry + stop_r * risk
        if new_stop <= current + 0.005:
            continue
        level = _first_cent(entry + trigger_r * risk)
        what = ("breakeven" if abs(stop_r) < 1e-9
                else f"entry {stop_r:+g}R")
        return (f"+{trigger_r:g}R at a 5-min close ≥ ${level:,.2f} "
                f"(from {p['not_before_et']} ET) → stop ${new_stop:,.2f} ({what})")
    return (f"Trailing: highest daily close − {p['trail_atr']:g}×ATR"
            f"{p['atr_length']}, never lowered")


def position_rows(broker_positions, local_positions, orders, config):
    """One exact row per broker position. Entry is the broker's average fill;
    the stop and target are the LIVE broker legs (falling back to the local
    record when a leg is not visible)."""
    legs = live_legs(orders)
    profiles = (config or {}).get("risk_profiles") or {}
    names = (config or {}).get("ticker_profiles") or {}
    rows = []
    for p in broker_positions or []:
        sym = p.symbol
        qty = abs(float(p.qty))
        entry = float(p.avg_entry_price)
        last = float(getattr(p, "current_price", 0) or 0)
        local = (local_positions or {}).get(sym) or {}
        leg = legs.get(sym, {})
        stop = leg.get("stop", local.get("trailing_stop_price"))
        target = leg.get("target", local.get("profit_target_price"))
        orig = pm.original_stop(local)
        risk_ps = (entry - orig) if orig is not None and orig < entry else None
        pnl = (last - entry) * qty
        if stop is None:
            stop_state = "no stop visible"
        elif orig is not None and abs(stop - orig) < 0.005:
            stop_state = "original"
        elif stop >= entry - 0.005 and stop <= entry + 0.005:
            stop_state = "breakeven"
        elif stop > entry:
            stop_state = "locked profit"
        else:
            stop_state = "raised (still below entry)"
        to_stop = (stop - entry) * qty if stop is not None else None
        rows.append({
            "Ticker": sym, "Setup": local.get("setup", "—"),
            "Qty": qty, "Entry (fill)": entry, "Last": last,
            "P&L $": pnl, "P&L %": (last / entry - 1) * 100 if entry else 0.0,
            "R now": (last - entry) / risk_ps if risk_ps else None,
            "Stop (live)": stop, "Stop is": stop_state,
            "If stopped $": to_stop,
            "Target": target,
            # a progress bar: below entry is 0% of the way, not negative
            "To target %": (min(100.0, max(0.0, (last - entry) / (target - entry) * 100))
                            if target and target > entry else None),
            "Next stop move": next_trigger(
                entry, orig, stop, local.get("highest_close") or last,
                profiles.get(names.get(sym, "Moderate"), {}))
            if local.get("timeframe") == "daily" else "intraday ATR trail",
            "Owner": local.get("source", "unknown"),
        })
    return rows


# ============================================================ decisions

def decision_rows(conn, day: str, threshold: float = 70):
    """Today's gatekeeper calls (Claude and the CEO), newest first. An
    approval under the conviction threshold is shown as not traded."""
    rows = conn.execute(
        "SELECT timestamp, ticker, setup_name, source, approved, "
        "conviction_score, json_extract(verdict,'$.rejection_reason') AS reason, "
        "json_extract(verdict,'$.reasoning') AS reasoning, "
        "json_extract(verdict,'$.error') AS error "
        "FROM decisions WHERE source IN ('claude','ceo') AND timestamp LIKE ? "
        "ORDER BY id DESC", (f"{day}%",)).fetchall()
    out = []
    for r in rows:
        conv = r["conviction_score"]
        if r["error"]:
            verdict = "error"
        elif not r["approved"]:
            verdict = "rejected"
        elif conv is not None and conv < threshold:
            verdict = f"approved, below {threshold:g} — not traded"
        else:
            verdict = "approved"
        out.append({"Time": str(r["timestamp"])[11:19], "Ticker": r["ticker"],
                    "Setup": r["setup_name"], "Verdict": verdict,
                    "Conviction": r["conviction_score"],
                    "Why": (r["error"] or r["reason"] or r["reasoning"] or "")[:160]})
    return out


# ============================================================ watchlist (S37)
# CEO 2026-10-21: one row per ticker the worker scanned in its LAST cycle,
# with its status, joined to today's universe (price, change, source) and
# the morning catalyst scan. Files only - no broker or API call.

WATCH_GROUPS = (                       # (group, rank, prefixes) - first match
    ("Position", 0, ("In Pos", "BOUGHT", "Entry pending", "SOLD", "CLOSED",
                     "Exited", "Order failed")),
    ("Gatekeeper", 1, ("Gatekeeper", "Waiting (gatekeeper")),
    ("Watch-only", 2, ("Pass (watch_only",)),
    ("Filtered", 3, ("Pass", "chop_reclaim", "duplicate blocked")),
    ("Waiting", 4, ("Waiting",)),
    ("No data", 5, ("No Data",)),
)


def last_cycle_line(status_file="bot_status.log") -> str:
    """The newest status line that is a cycle line (the file also holds
    'Market is CLOSED' / 'Session ended' lines)."""
    try:
        with open(status_file, encoding="utf-8", errors="replace") as f:
            for line in f.read().splitlines():
                if "Cycle #" in line and "|" in line:
                    return line
    except OSError:
        pass
    return ""


def cycle_statuses(line: str) -> list:
    """[(ticker, status)] in scan order. A ticker can appear twice in one
    cycle (e.g. 'chop_reclaim (Rule #5)' then 'Gatekeeper blocked (...)');
    both are kept, in order."""
    m = _STATUS.match(line or "")
    body = m.group("body") if m else (line or "")
    out = []
    for part in body.split(" | ")[1:]:
        if "Monitoring Live" in part and "): " in part:
            part = part.split("): ", 1)[1]
        tk, sep, status = part.partition(": ")
        if sep and tk.strip() and " " not in tk.strip():
            out.append((tk.strip(), status.strip()))
    if " | " in body:
        head = body.split(" | ")[0]
        if "): " in head:
            tk, _, status = head.split("): ", 1)[1].partition(": ")
            if tk.strip() and " " not in tk.strip():
                out.insert(0, (tk.strip(), status.strip()))
    return out


def status_group(status: str):
    for group, rank, prefixes in WATCH_GROUPS:
        if any(status.startswith(p) for p in prefixes):
            return group, rank
    return "Other", 6


def _detail(status: str) -> str:
    """'Pass (trend_continuation: adx_low)' -> 'trend_continuation: adx_low'."""
    s = status.strip()
    if s.startswith("In Pos"):
        return s[len("In Pos"):].strip()          # '[bot] (+0.38%)'
    if s.startswith("Waiting (No Signal)"):
        return ""
    if "(" in s and s.endswith(")"):
        return s[s.index("(") + 1:-1]
    return "" if s.startswith("Waiting (No Signal)") else s


def watchlist_rows(status_file="bot_status.log",
                   universe_file="universe_today.json",
                   catalysts_file="catalysts_today.json", today=None) -> dict:
    """{'cycle', 'time', 'rows': [...]} - rows sorted: positions, gatekeeper,
    watch-only, filtered, waiting, no data; then by |change %|. The
    universe and catalyst files are read for the cycle's own date."""
    line = last_cycle_line(status_file)
    summary = status_summary(line)
    today = today or (summary["time"] or "")[:10] or None

    def load(path):
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError):
            return {}
        if today and payload.get("date") != today:
            return {}
        return payload
    uni = {c.get("symbol"): c for c in load(universe_file).get("candidates", [])}
    cat = {n.get("symbol"): n for n in load(catalysts_file).get("names", [])}

    merged = {}
    for tk, status in cycle_statuses(line):
        group, rank = status_group(status)
        if tk in merged:              # keep the furthest stage, join details
            prev = merged[tk]
            if rank < prev["_rank"]:
                prev.update(Status=group, _rank=rank)
            prev["Detail"] = "; ".join(x for x in (prev["Detail"], _detail(status)) if x)
            continue
        merged[tk] = {"Ticker": tk, "Status": group, "_rank": rank,
                      "Detail": _detail(status)}
    rows = []
    for tk, r in merged.items():
        u, c = uni.get(tk, {}), cat.get(tk, {})
        catalyst = bool(c) or bool(u.get("catalyst"))
        rows.append({
            "Ticker": tk, "Status": r["Status"], "Detail": r["Detail"][:140],
            "Price": u.get("price"),
            "Change %": (round(float(u["change_pct"]), 2)
                         if u.get("change_pct") is not None else None),
            "Source": "catalyst" if catalyst and u.get("source") in (None, "catalyst")
                      else (u.get("source") or ("held" if r["_rank"] == 0 else "config")),
            "Watch-only": bool(u.get("watch_only")) or bool(c),
            "Catalyst": (c.get("tag") or u.get("catalyst_tag") or "") if catalyst else "",
            "Headline": ((c.get("headline") or u.get("catalyst_headline") or "")[:120]
                         if catalyst else ""),
            "_rank": r["_rank"],
        })
    rows.sort(key=lambda r: (r["_rank"], -abs(r["Change %"] or 0), r["Ticker"]))
    for r in rows:
        r.pop("_rank")
    return {"cycle": summary["cycle"], "time": summary["time"], "rows": rows}
