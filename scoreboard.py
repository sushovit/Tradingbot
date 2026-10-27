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

Three numbers per trade (CEO 2026-10-21 fix):
  LIVE $   what the account actually made (journal realized / broker
           unrealized) - for reference only: it carries every live accident.
  RULE $   the CURRENT live R/ATR ladder replayed on the same bars from the
           same entry, original stop and target (lock_replay.replay_r_atr).
  OWNER $  the owner's 0.5% ladder replayed the same way.
The comparison is RULES vs RULES: OWNER $ - RULE $. A trade touched by a
journaled bug (ops events fill_corrected / entry bug) is marked with a dagger.

The switch rule (agenda 25): switch to the owner ladder if it beats the
live rule by more than $0 in total over at least 10 CLOSED trades. The
decision is the CEO's: the Friday post ends "CEO action: switch" or "CEO
action: none". The owner never edits the config.
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
# ops-event kinds that mean a trade's live numbers were bent by a bug
BUG_KINDS = ("fill_corrected", "entry_reconciled", "entry_bug",
             "entry_fill_corrected")
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


def bug_flags(conn, trades) -> set:
    """buy_ts of every trade an ops bug event touched: the event names the
    ticker (first word of its target) and falls between the buy and 3 days
    after the sell (now, for an open trade)."""
    flags = set()
    try:
        rows = conn.execute(
            "SELECT ticker, substr(timestamp,1,10) FROM decisions WHERE source='ops' "
            "AND setup_name IN (%s)" % ",".join("?" * len(BUG_KINDS)), BUG_KINDS).fetchall()
    except Exception:
        return flags
    for t in trades:
        start = t["buy_ts"][:10]
        end = "9999-12-31"
        if t.get("sell_ts"):
            from datetime import timedelta
            end = (date.fromisoformat(t["sell_ts"][:10]) + timedelta(days=3)).isoformat()
        for target, day in rows:
            if str(target or "").split()[:1] == [t["ticker"]] and start <= day <= end:
                flags.add(t["buy_ts"])
    return flags


def score(trades, open_positions, bars_for, params, positions=None,
          rule_params=None, atr_for=None, flags=()):
    """Rows {ticker, buy_ts, status, live, rule, owner, diff, *_open, bug}
    and totals. diff = OWNER $ - RULE $ (rules vs rules)."""
    import lock_replay
    import position_mgmt as pm
    policy = lock_replay.shadow_ladder_policy(params)
    rule_params = rule_params or dict(pm.R_ATR_DEFAULTS)
    positions = positions or {}
    rows = []
    for t in trades:
        closed = t["sell"] is not None and t["reason"] != "partial"
        state = positions.get(t["ticker"]) or {}
        stop = t.get("orig_stop") or state.get("original_stop") or state.get("initial_stop")
        target = (t.get("target") if closed
                  else state.get("profit_target_price") or t.get("target"))
        target = float(target) if target else None
        live = t["pnl"] if closed else (open_positions.get(t["ticker"]) or {}).get("unrealized")
        owner = rule = owner_open = rule_open = None
        if stop is not None:
            try:
                bars = bars_for(t["ticker"], t["buy_ts"])
            except Exception:
                bars = None
            if bars is not None and not getattr(bars, "empty", True):
                px, ts, _ = lock_replay.replay(bars, t["buy_ts"], t["buy"],
                                               float(stop), target, policy)
                if px is not None:
                    owner, owner_open = (px - t["buy"]) * t["qty"], ts is None
                try:
                    atr = atr_for(t["ticker"], t["buy_ts"]) if atr_for else None
                except Exception:
                    atr = None
                px, ts, _ = lock_replay.replay_r_atr(bars, t["buy_ts"], t["buy"],
                                                     float(stop), target,
                                                     rule_params, atr)
                if px is not None:
                    rule, rule_open = (px - t["buy"]) * t["qty"], ts is None
        rows.append({"ticker": t["ticker"], "buy_ts": t["buy_ts"],
                     "status": "closed" if closed else "open", "live": live,
                     "rule": rule, "owner": owner, "rule_open": rule_open,
                     "owner_open": owner_open, "bug": t["buy_ts"] in flags,
                     "diff": (owner - rule) if owner is not None and rule is not None else None})
    both = [r for r in rows if r["diff"] is not None]
    closed_rows = [r for r in both if r["status"] == "closed"]
    totals = {
        "live": sum(r["live"] or 0.0 for r in rows),
        "rule": sum(r["rule"] for r in both), "owner": sum(r["owner"] for r in both),
        "closed_n": len(closed_rows),
        "closed_rule": sum(r["rule"] for r in closed_rows),
        "closed_owner": sum(r["owner"] for r in closed_rows),
        "unscored": [r["ticker"] for r in rows if r["diff"] is None],
    }
    totals["closed_diff"] = totals["closed_owner"] - totals["closed_rule"]
    totals["rule_met"] = (totals["closed_n"] >= MIN_CLOSED and totals["closed_diff"] > 0)
    return rows, totals


def _m(x):
    return "-" if x is None else ("-" if x < 0 else "+") + f"{abs(x):,.2f}"


# CEO 2026-10-22 (after KMI/SLB: Claude approved both, the shadow said no to
# both): every account trade split by the SHADOW model's verdict on the same
# signal - its local_shadow row for the ticker logged within 15 minutes after
# the gatekeeper call. P&L to date: realized, or unrealized while open.
SHADOW_MATCH_MIN = 15


def shadow_verdict(conn, decision_id):
    """'yes' / 'no' / None (no verdict, or the shadow errored)."""
    if decision_id is None:
        return None
    row = conn.execute("SELECT ticker, timestamp FROM decisions WHERE id=?",
                       (decision_id,)).fetchone()
    if not row:
        return None
    ticker, ts = row
    hit = conn.execute(
        "SELECT approved, verdict FROM decisions WHERE source='local_shadow' AND ticker=? "
        "AND timestamp >= ? AND timestamp <= datetime(?, ?) ORDER BY id LIMIT 1",
        (ticker, ts, ts, f"+{SHADOW_MATCH_MIN} minutes")).fetchone()
    if not hit:
        return None
    try:
        if "error" in json.loads(hit[1] or "{}"):
            return None
    except ValueError:
        return None
    return "yes" if hit[0] else "no"


def shadow_split(conn, open_positions):
    """{'yes'|'no'|'none': {'n', 'pnl', 'tickers'}} over every trade on the
    $5,000 account since it started (all setups)."""
    out = {k: {"n": 0, "pnl": 0.0, "tickers": []} for k in ("no", "yes", "none")}
    for t in trade_table.load_trades(conn):
        if t["buy_ts"] < trade_table.ACCOUNT_START:
            continue
        if str(t.get("setup") or "").upper().startswith("INTERN"):
            continue
        closed = t["sell"] is not None and t["reason"] != "partial"
        pnl = t["pnl"] if closed else (open_positions.get(t["ticker"]) or {}).get("unrealized")
        did = conn.execute("SELECT decision_id FROM trades WHERE action='BUY' AND ticker=? "
                           "AND timestamp=?", (t["ticker"], t["buy_ts"])).fetchone()
        key = shadow_verdict(conn, did[0] if did else None) or "none"
        out[key]["n"] += 1
        out[key]["pnl"] += pnl or 0.0
        out[key]["tickers"].append(t["ticker"])
    return out


def shadow_lines(split):
    if not split:
        return []
    lab = {"no": "Shadow said NO ", "yes": "Shadow said YES", "none": "No shadow verdict"}
    lines = ["Shadow vs trades (since 10-09, running):"]
    for k in ("no", "yes", "none"):
        v = split[k]
        if v["n"] or k != "none":
            lines.append(f"  {lab[k]}: {v['n']} tr {_m(v['pnl'])}")
    return lines


def attach_cap3(rows, conn):
    """CEO 2026-10-27: the 3.0CAP column - each trade's result with the OLD
    3.0 x ATR target (the S56b paper twin, cap_shadow). Trades entered while
    3.0 was the live cap (before cap_shadow.START) are the same trade: '='."""
    import cap_shadow
    twins = cap_shadow.by_entry(conn)
    for r in rows:
        t = twins.get((r["ticker"], r["buy_ts"]))
        if t is not None:
            r["cap3"], r["cap3_mark"] = t["twin_pnl"], ("*" if t["status"] in
                                                       (cap_shadow.OPEN, cap_shadow.RUNNING) else "")
        elif r["buy_ts"][:10] < cap_shadow.START:
            r["cap3"], r["cap3_mark"] = r["live"], "="
        else:
            r["cap3"], r["cap3_mark"] = None, ""
    return rows


def cap3_lines(rows):
    import cap_shadow
    new = [r for r in rows if r.get("cap3") is not None and r.get("cap3_mark") != "="]
    if not any("cap3" in r for r in rows):
        return []
    done = sum(1 for r in new if r["status"] == "closed" and r.get("cap3_mark") != "*")
    diff = sum((r["cap3"] or 0) - (r["live"] or 0) for r in new)
    return ["3.0CAP = same trade, OLD 3.0 x ATR target",
            "(S56b paper twin; = : 3.0 was live then)",
            f"3.0 cap - live since {cap_shadow.START[5:]}: {_m(diff)} "
            f"({len(new)} tr)",
            (f"3.0 cap review: DUE ({done} closed)" if done >= cap_shadow.REVIEW_AFTER
             else f"3.0 cap review at {done}/{cap_shadow.REVIEW_AFTER} closed")]


def table_lines(rows, totals, day, shadow=None):
    cap3 = any("cap3" in r for r in rows)
    w = max(trade_table.WIDTH, 54) if cap3 else trade_table.WIDTH
    mw = 7 if cap3 else 8
    head = (f"{'TICK':<5} {'IN':<5} {'S':<1} {'LIVE $':>{mw}} {'RULE $':>{mw}} "
            f"{'OWNER $':>{mw}} {'DIFF':>{mw}}" + (f" {'3.0CAP':>{mw}}" if cap3 else ""))
    lines = [head, "-" * w]
    for r in rows:
        tk = r["ticker"][:4] + ("†" if r["bug"] else "")
        c3 = ""
        if cap3:
            c3 = " " + (("=" if r.get("cap3_mark") == "=" else
                         _m(r.get("cap3")) + r.get("cap3_mark", ""))).rjust(mw)
        lines.append(f"{tk:<5} {r['buy_ts'][5:10]:<5} {r['status'][0].upper():<1} "
                     f"{_m(r['live']):>{mw}} "
                     f"{(_m(r['rule']) + ('*' if r['rule_open'] else '')):>{mw}} "
                     f"{(_m(r['owner']) + ('*' if r['owner_open'] else '')):>{mw}} "
                     f"{_m(r['diff']):>{mw}}{c3}"[:w])
    if not rows:
        lines.append("(no daily trades on the account yet)")
    lines.append("-" * w)
    c3t = ""
    if cap3:
        c3t = f" {_m(sum((r.get('cap3') or 0) for r in rows)):>{mw}}"
    lines.append(f"{'TOTAL':<13} {_m(totals['live']):>{mw}} {_m(totals['rule']):>{mw}} "
                 f"{_m(totals['owner']):>{mw}} {_m(totals['owner'] - totals['rule']):>{mw}}{c3t}"[:w])
    lines.append("DIFF = OWNER - RULE (rules vs rules);")
    lines.append("LIVE = what the account made (reference).")
    lines += cap3_lines(rows)
    lines.append(f"Closed: {totals['closed_n']}  owner - rule "
                 f"{_m(totals['closed_diff'])}")
    if any(r["rule_open"] or r["owner_open"] for r in rows):
        lines.append("* rule still holding: valued at last close")
    if any(r["bug"] for r in rows):
        lines.append("† live result bent by a journaled bug")
    if totals["unscored"]:
        lines.append("No replay for: " + ", ".join(totals["unscored"]))
    lines += shadow_lines(shadow)
    lines.append(f"Switch if owner > rule over >= {MIN_CLOSED} closed.")
    if totals["rule_met"]:
        lines.append(f"Rule MET ({totals['closed_n']} closed).")
        lines.append("CEO action: switch")
    else:
        need = max(0, MIN_CLOSED - totals["closed_n"])
        lines.append("Rule not met" + (f" ({need} more closed needed)." if need
                                       else " (owner not ahead)."))
        lines.append("CEO action: none")
    return [ln[:w] for ln in lines]


def build(day, conn, broker, config, positions):
    import position_mgmt as pm
    trades = daily_trades(conn, config)
    open_positions = trade_table.live_open_positions(broker) if broker else {}

    def bars_for(ticker, since):
        days = (day - date.fromisoformat(since[:10])).days + 3
        return broker.get_bars([ticker], timeframe_minutes=5,
                               lookback_days=max(days, 3)).get(ticker)

    def atr_for(ticker, since):
        daily = broker.get_daily_bars([ticker], lookback_days=
                                      (day - date.fromisoformat(since[:10])).days + 60)
        return pm.daily_atr(daily.get(ticker), 14, since[:10])
    profile = ((config.get("risk_profiles") or {}).get("Moderate") or {})
    return score(trades, open_positions, bars_for, owner_params(config), positions,
                 rule_params=pm.r_atr_params(profile), atr_for=atr_for,
                 flags=bug_flags(conn, trades))


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
        broker = Broker()
        rows, totals = build(day, conn, broker, config, positions)
        try:
            attach_cap3(rows, conn)                  # CEO 2026-10-27: 3.0CAP column
        except Exception as e:
            print(f"3.0CAP column skipped: {e}")
        try:
            split = shadow_split(conn, trade_table.live_open_positions(broker))
        except Exception:
            split = None
    finally:
        conn.close()
    lines = table_lines(rows, totals, day, split)
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
