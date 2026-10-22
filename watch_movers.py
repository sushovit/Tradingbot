"""
watch_movers.py — CEO 2026-10-23: the Qwen watchlist. VIEW ONLY.

    python watch_movers.py [--no-post]

launchd com.tradingbot.watch_movers, every 30 min 10:00-15:00 ET Mon-Fri:
  1. Alpaca market-wide top movers + most-actives; snapshots give price and
     % change vs the prior close. Keep price 5-1200, 20-session ADV >= $20M,
     |change| >= 3%, tradable non-OTC non-ETF; at most 10 names not already
     posted today, largest |change| first.
  2. The local model (models.shadow, qwen3.5:9b) writes one line per name
     from its Finnhub headlines and sector: facts only, <= 12 words. It
     YIELDS to the shadow queue (waits while shadow_busy.marker is fresh),
     30 s per call, fallback "no note".
  3. Discord "👀 Watching": ticker, % change, note, why it was not traded
     (not in list / no setup / the blocker). Each name is journaled as a
     decisions row with source='watch' and its price; outcomes.py grades it
     at +1 / +3 / +5 sessions.
NEVER TRADABLE: nothing here writes the universe or reaches the order path.
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta

import pytz

logger = logging.getLogger("watch_movers")
ET = pytz.timezone("America/New_York")
TITLE = "👀 Watching"
NO_NOTE = "no note"
DEFAULTS = {"min_price": 5.0, "max_price": 1200.0, "min_dollar_volume": 20_000_000,
            "min_change_pct": 3.0, "max_names": 10, "note_timeout_s": 30,
            "note_words": 12, "yield_wait_s": 60}
NOTE_SYSTEM = (
    "You write one factual line about why a stock is moving today, from the "
    "headlines given. Facts only: no opinion, no advice, no forecast. At most "
    "12 words. If the headlines do not explain the move, say what they report. "
    "Answer with the line only.")


def settings(config=None):
    cfg = dict(DEFAULTS)
    cfg.update(((config or {}).get("watch_movers") or {}))
    return cfg


def load_config():
    try:
        with open("bot_config.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def today_et(now=None):
    return (now or datetime.now(ET)).strftime("%Y-%m-%d")


# ============================================================ selection

def posted_today(conn, day) -> set:
    try:
        return {r[0] for r in conn.execute(
            "SELECT ticker FROM decisions WHERE source='watch' AND substr(timestamp,1,10)=?",
            (day,))}
    except sqlite3.Error:
        return set()


def candidates(broker, config, already=(), now=None):
    """[{symbol, price, prev_close, change_pct, adv_usd, name}] passing every
    filter, largest |change| first, at most max_names, none posted today."""
    import catalysts
    cfg = settings(config)
    pool = []
    try:
        pool += [m["symbol"] for m in broker.get_market_movers(top=50)]
    except Exception as e:
        logger.warning(f"market movers unavailable: {e}")
    try:
        pool += [a["symbol"] for a in broker.get_most_actives(top=50)]
    except Exception as e:
        logger.warning(f"most-actives unavailable: {e}")
    pool = [s for s in dict.fromkeys(pool) if s and s not in already]
    rows = []
    for sym, (px, pc) in catalysts.snapshots(broker, pool).items():
        chg = catalysts.gap_pct(px, pc)
        if chg is None or abs(chg) < cfg["min_change_pct"]:
            continue
        if not (cfg["min_price"] <= px <= cfg["max_price"]):
            continue
        rows.append({"symbol": sym, "price": px, "prev_close": pc,
                     "change_pct": round(chg, 2)})
    if rows:
        try:
            daily = broker.get_daily_bars([r["symbol"] for r in rows], lookback_days=40)
        except Exception as e:
            logger.warning(f"daily bars unavailable: {e}")
            daily = {}
        for r in rows:
            r["adv_usd"] = catalysts.avg_dollar_volume(daily.get(r["symbol"]))
    rows = [r for r in rows if r.get("adv_usd") and r["adv_usd"] >= cfg["min_dollar_volume"]]
    rows = catalysts.tradable_only(broker, rows, config)
    rows.sort(key=lambda r: (-abs(r["change_pct"]), r["symbol"]))
    return rows[: int(cfg["max_names"])]


# ============================================================ why not traded

def why_not_traded(ticker, universe_tickers, cycle_line) -> str:
    """'not in list' / 'no setup' / the blocker, from today's universe and the
    worker's last cycle line."""
    import dashboard_data as dd
    if ticker not in set(universe_tickers or ()):
        return "not in list"
    seen = [s for t, s in dd.cycle_statuses(cycle_line) if t == ticker]
    if not seen:
        return "not scanned yet"
    status = seen[-1]
    group, _ = dd.status_group(status)
    detail = dd._detail(status)
    return {"Waiting": "no setup",
            "Position": "held",
            "Watch-only": "watch-only (catalyst)",
            "No data": "no data"}.get(group, f"{group.lower()}: {detail}" if detail else group.lower())


# ============================================================ the note

def shadow_busy(path=None, fresh_s=90) -> bool:
    import analyst
    path = path or analyst.SHADOW_BUSY_FILE
    try:
        return time.time() - os.path.getmtime(path) < fresh_s
    except OSError:
        return False


def clip_words(text, n=12) -> str:
    words = re.sub(r"\s+", " ", str(text or "")).strip().strip('"').split(" ")
    words = [w for w in words if w]
    return " ".join(words[:n])


def write_note(ticker, headlines, sector, config=None, post=None, busy=None,
               sleep=time.sleep) -> str:
    """One local-model line, <= 12 words; NO_NOTE on any failure, timeout, or
    when the shadow queue stays busy past yield_wait_s."""
    cfg = settings(config)
    if not headlines:
        return NO_NOTE
    busy = busy or shadow_busy
    waited = 0.0
    while busy():
        if waited >= cfg["yield_wait_s"]:
            return NO_NOTE
        sleep(5)
        waited += 5
    try:
        import local_analyst
        import requests
        post = post or requests.post
        user = (f"{ticker} ({sector or 'sector unknown'}). Headlines:\n"
                + "\n".join(f"- {h}" for h in headlines[:5]))
        resp = post(f"{local_analyst.OLLAMA_URL}/api/chat",
                    json={"model": local_analyst.shadow_model(config),
                          "messages": [{"role": "system", "content": NOTE_SYSTEM},
                                       {"role": "user", "content": user}],
                          "stream": False, "think": False,
                          "keep_alive": local_analyst.KEEP_ALIVE,
                          # same context as the shadow: no model reload
                          "options": {"temperature": 0, "num_ctx": local_analyst.NUM_CTX}},
                    timeout=cfg["note_timeout_s"])
        resp.raise_for_status()
        line = clip_words(resp.json()["message"]["content"], cfg["note_words"])
        return line or NO_NOTE
    except Exception as e:
        logger.warning(f"{ticker}: no note ({e})")
        return NO_NOTE


def headlines_for(client, ticker, now):
    import catalysts
    if client is None:
        return []
    since = (now - timedelta(days=3)).strftime("%Y-%m-%d")
    items = catalysts.recent_news(catalysts.company_news(client, ticker, since,
                                                         now.strftime("%Y-%m-%d")),
                                  now.astimezone(pytz.utc), 24)
    return [n.get("headline", "").strip() for n in items[:5] if n.get("headline")]


# ============================================================ outputs

def journal_rows(rows):
    import journal
    ids = []
    for r in rows:
        ids.append(journal.log_decision(
            r["symbol"], "watch",
            {k: r.get(k) for k in ("price", "prev_close", "change_pct", "adv_usd",
                                   "note", "why", "sector", "headline")},
            {"watch": True, "note": r.get("note"),
             "reasoning": "Watchlist only - never tradable."},
            source="watch"))
    return ids


def table_lines(rows, now):
    import trade_table
    w = trade_table.WIDTH
    if not rows:
        return [f"No new movers at {now:%H:%M} ET (|chg| >= 3%, ADV >= $20M)."]
    lines = [f"{'TICK':<5} {'CHG%':>6}  WHY NOT TRADED", "-" * w]
    for r in rows:
        lines.append(f"{r['symbol'][:5]:<5} {r['change_pct']:>+6.1f}  {r['why']}"[:w])
        import textwrap
        lines += [f"      {part}" for part in textwrap.wrap(r["note"], w - 6)[:2]]
    lines += ["-" * w, "View only - never traded from this list."]
    return lines


def post(rows, now):
    import alerts
    import trade_alerts
    import trade_table
    if not trade_alerts.enabled("watching"):
        return 0
    msgs = trade_table.to_messages(f"{TITLE} — {now:%a %H:%M} ET", table_lines(rows, now))
    for m in msgs:
        alerts.post_async(m)
    return len(msgs)


def run(broker, client, config, conn, now=None, note_fn=None):
    """The 30-minute job body. Returns the rows posted."""
    import dashboard_data as dd
    import sectors
    import universe
    now = now or datetime.now(ET)
    day = today_et(now)
    rows = candidates(broker, config, posted_today(conn, day), now)
    uni = universe.load_universe_tickers()
    line = dd.last_cycle_line("bot_status.log")
    if line and not line[1:11] == day:
        line = ""                                    # another day's cycle
    note_fn = note_fn or (lambda t, hs, sec: write_note(t, hs, sec, config))
    for r in rows:
        hs = headlines_for(client, r["symbol"], now)
        r["sector"] = sectors.sector_for(r["symbol"], r.get("name", ""))
        r["headline"] = hs[0] if hs else None
        r["note"] = note_fn(r["symbol"], hs, r["sector"])
        r["why"] = why_not_traded(r["symbol"], uni, line)
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Qwen watchlist (view only)")
    ap.add_argument("--no-post", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from dotenv import load_dotenv
    load_dotenv()
    import catalysts
    import journal
    from broker import Broker
    config = load_config()
    if not (config.get("watch_movers") or {}).get("enabled", True):
        print("watch_movers disabled in bot_config.json")
        return 0
    now = datetime.now(ET)
    conn = sqlite3.connect(f"file:{journal.DB_FILE}?mode=ro", uri=True)
    try:
        rows = run(Broker(), catalysts.finnhub_client(), config, conn, now)
    finally:
        conn.close()
    for line in table_lines(rows, now):
        print(line)
    if a.no_post:
        return 0
    journal_rows(rows)
    import alerts
    post(rows, now)
    alerts.drain()
    return 0


if __name__ == "__main__":
    sys.exit(main())
