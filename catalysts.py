"""
catalysts.py — S36 (CEO 2026-10-21): the morning catalyst scanner.

    python catalysts.py            # 09:00 ET: scan, tag, hand names to the universe
    python catalysts.py --post     # 09:20 ET: post the "Morning catalysts" table
    python catalysts.py --dry-run  # scan and print; writes nothing, posts nothing

Scan (09:00 ET, before the 09:25 worker start):
  1. Pool: the universe scan pool (LIQUID_POOL + core watchlist), Alpaca's
     market movers, and the tickers Finnhub's market news names.
  2. Alpaca snapshots (IEX, the live feed): pre-market gap % = latest trade
     vs the previous session's close. Keep |gap| >= 3% and price 5-1200.
  3. ADV: 20-session average dollar volume from Alpaca daily bars >= $20M.
  4. Finnhub company news since the previous close; keep a name only if it
     has a headline in the last 18 h. Largest |gap| first, at most 15.
  5. The local model (qwen3.5:9b, models.shadow) tags each name's headline:
     earnings / upgrade-downgrade / FDA-clinical / M&A / guidance / other.
     30 s per call; any failure is "untagged".
  6. catalysts_today.json is written; universe.refresh() (the worker's
     09:25 start) merges today's names into universe_today.json, and if
     today's universe file already exists they are added to it now. Each
     name is journaled as a decisions row with source='catalyst'.

NO TRADES FROM NEWS ALONE. A catalyst name only joins the universe: it
trades if, and only if, a normal setup fires on it and clears every
normal gate. Nothing here imports the order path.
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pytz

logger = logging.getLogger("catalysts")

ET = pytz.timezone("America/New_York")
CONFIG_FILE = "bot_config.json"
CATALYSTS_FILE = "catalysts_today.json"
TAGS = ("earnings", "upgrade-downgrade", "FDA-clinical", "M&A", "guidance",
        "other")
UNTAGGED = "untagged"
TITLE = "📰 Morning catalysts"
DEFAULTS = {"min_gap_pct": 3.0, "news_hours": 18, "min_price": 5.0,
            "max_price": 1200.0, "min_dollar_volume": 20_000_000,
            "max_names": 15, "tag_timeout_s": 30}


def settings(config: dict = None) -> dict:
    cfg = dict(DEFAULTS)
    cfg.update(((config or {}).get("catalysts") or {}))
    return cfg


def load_config() -> dict:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def today_et(now=None) -> str:
    return (now or datetime.now(ET)).astimezone(ET).strftime("%Y-%m-%d")


# ============================================================ pure filters

def gap_pct(price, prev_close):
    try:
        price, prev_close = float(price), float(prev_close)
    except (TypeError, ValueError):
        return None
    if prev_close <= 0 or price <= 0:
        return None
    return 100.0 * (price / prev_close - 1.0)


def avg_dollar_volume(daily_df, sessions: int = 20):
    """Mean close x volume over the last `sessions` COMPLETED sessions."""
    if daily_df is None or len(daily_df) == 0:
        return None
    df = daily_df
    today = datetime.now(ET).strftime("%Y-%m-%d")
    if str(df.index[-1])[:10] == today:
        df = df.iloc[:-1]
    tail = df.tail(sessions)
    if len(tail) == 0:
        return None
    return float((tail["close"] * tail["volume"]).mean())


def recent_news(items, now_utc, hours: float):
    """Finnhub news items (epoch-second `datetime`) inside the last
    `hours`, newest first."""
    cutoff = now_utc.timestamp() - hours * 3600
    keep = [n for n in (items or [])
            if isinstance(n, dict) and float(n.get("datetime") or 0) >= cutoff
            and (n.get("headline") or "").strip()]
    return sorted(keep, key=lambda n: -float(n.get("datetime") or 0))


def best_headline(news, symbol: str, name: str = ""):
    """The newest headline that names the company (ticker, or the first
    word of its name); else the newest. Finnhub's company feed also carries
    sector stories that only mention the name in passing."""
    words = [symbol.upper()]
    first = re.sub(r"[^A-Za-z0-9&]", " ", name or "").split()
    if first and len(first[0]) >= 3 and first[0].upper() not in ("THE", "INC"):
        words.append(first[0].upper())
    for n in news:
        text = " " + re.sub(r"[^A-Za-z0-9&]", " ", n.get("headline", "")).upper() + " "
        if any(f" {w} " in text for w in words):
            return n
    return news[0]


def select(rows, cfg):
    """Rows {symbol, price, prev_close, gap_pct, adv_usd, news} that pass
    every filter, largest |gap| first, at most max_names."""
    keep = []
    for r in rows:
        g, p, adv = r.get("gap_pct"), r.get("price"), r.get("adv_usd")
        if g is None or abs(g) < cfg["min_gap_pct"]:
            continue
        if p is None or not (cfg["min_price"] <= p <= cfg["max_price"]):
            continue
        if adv is None or adv < cfg["min_dollar_volume"]:
            continue
        if not r.get("news"):
            continue
        keep.append(r)
    keep.sort(key=lambda r: (-abs(r["gap_pct"]), r["symbol"]))
    return keep[: int(cfg["max_names"])]


# ============================================================ tagging

TAG_SYSTEM = (
    "You classify one stock-news headline. Answer with JSON only: "
    '{"tag": "<one of: earnings, upgrade-downgrade, FDA-clinical, M&A, '
    'guidance, other>"}. earnings = quarterly results or an earnings date; '
    "upgrade-downgrade = an analyst rating or price-target change; "
    "FDA-clinical = drug approval, trial data or a regulator's medical "
    "decision; M&A = merger, acquisition, takeover, spin-off or stake sale; "
    "guidance = company outlook/forecast raised, cut or reaffirmed; "
    "other = anything else.")


def normalise_tag(raw) -> str:
    t = str(raw or "").strip().lower().replace("_", "-").replace(" ", "-")
    for tag in TAGS:
        if t == tag.lower():
            return tag
    aliases = {"upgrade": "upgrade-downgrade", "downgrade": "upgrade-downgrade",
               "fda": "FDA-clinical", "clinical": "FDA-clinical",
               "merger": "M&A", "acquisition": "M&A", "m-and-a": "M&A",
               "m&a": "M&A", "outlook": "guidance"}
    return aliases.get(t, UNTAGGED)


def tag_headline(headline: str, ticker: str = "", config: dict = None,
                 timeout: float = None, post=None) -> str:
    """One local-model call; "untagged" on ANY failure or timeout."""
    cfg = settings(config)
    timeout = timeout or cfg["tag_timeout_s"]
    try:
        import local_analyst
        import requests
        post = post or requests.post
        resp = post(
            f"{local_analyst.OLLAMA_URL}/api/chat",
            json={"model": local_analyst.shadow_model(config),
                  "messages": [{"role": "system", "content": TAG_SYSTEM},
                               {"role": "user",
                                "content": f"{ticker}: {headline}"}],
                  "stream": False, "format": "json", "think": False,
                  "keep_alive": local_analyst.KEEP_ALIVE,
                  "options": {"temperature": 0, "num_ctx": 1024}},
            timeout=timeout)
        resp.raise_for_status()
        content = resp.json()["message"]["content"]
        m = re.search(r"\{.*\}", content, re.S)
        return normalise_tag(json.loads(m.group(0)).get("tag") if m else None)
    except Exception as e:
        logger.warning(f"{ticker}: headline not tagged ({e})")
        return UNTAGGED


# ============================================================ data (network)

def finnhub_client():
    key = os.getenv("FINNHUB_API_KEY")
    if not key:
        return None
    import finnhub
    return finnhub.Client(api_key=key)


_TICKER_IN_TEXT = re.compile(r"\(([A-Z]{1,5}(?:\.[A-Z])?)\)|\$([A-Z]{1,5})\b")


def tickers_in_text(text: str) -> list:
    """'3M (MMM) jumps', '$TSLA rallies' -> ['MMM', 'TSLA']."""
    return [a or b for a, b in _TICKER_IN_TEXT.findall(text or "")]


_SUFFIXES = {"INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY",
             "LTD", "LIMITED", "PLC", "HOLDINGS", "HOLDING", "GROUP", "SA", "NV",
             "AG", "LP", "LLC", "THE", "CLASS", "A", "B", "C", "COMMON", "STOCK",
             "SHARES", "ORDINARY", "ADR", "ADS", "DEPOSITARY", "AMERICAN",
             "SPONSORED", "NEW", "DE", "CL"}


def clean_company(name: str) -> str:
    """'United Airlines Holdings, Inc. Common Stock' -> 'UNITED AIRLINES'."""
    words = re.sub(r"[^A-Za-z0-9&' ]", " ", name or "").upper().split()
    while words and words[-1] in _SUFFIXES:
        words.pop()
    if words and words[0] == "THE":
        words = words[1:]
    return " ".join(words)


def name_index(assets) -> dict:
    """{cleaned company name: symbol} for names distinctive enough to match
    in a headline: two or more words, or one word of 6+ letters. A
    same-name collision keeps neither."""
    idx, clash = {}, set()
    for a in assets or []:
        sym, nm = a.get("symbol"), clean_company(a.get("name", ""))
        if not sym or not nm or "." in sym and not sym.endswith((".A", ".B")):
            continue
        if len(nm.split()) < 2 and len(nm) < 6:
            continue
        if nm in idx and idx[nm] != sym:
            clash.add(nm)
        idx[nm] = sym
    for nm in clash:
        idx.pop(nm, None)
    return idx


def tickers_by_name(text: str, index: dict) -> list:
    t = " " + re.sub(r"[^A-Za-z0-9&' ]", " ", text or "").upper() + " "
    t = re.sub(r"'S ", " ", t)
    return [sym for nm, sym in index.items() if f" {nm} " in t]


def active_assets(broker):
    """Every active US equity's {symbol, name}: one raw /assets call (the
    SDK cannot parse the list since 2026-10-20)."""
    try:
        rows = broker.trading.get("/assets", {"status": "active",
                                              "asset_class": "us_equity"})
        return [{"symbol": r.get("symbol"), "name": r.get("name")}
                for r in rows or [] if r.get("tradable")
                and r.get("exchange") in ("NYSE", "NASDAQ", "ARCA", "AMEX", "BATS")]
    except Exception as e:
        logger.warning(f"asset names unavailable: {e}")
        return []


def market_news_tickers(client, broker=None) -> list:
    """Tickers Finnhub's general market news names: its `related` field
    (empty on the free feed, 2026-10-21: 0 of 100 items) plus tickers
    written in the headline/summary. Kept only if Alpaca knows the symbol,
    so an invalid one cannot break a snapshot batch."""
    out = []
    index = name_index(active_assets(broker)) if broker is not None else {}
    try:
        for n in client.general_news("general", min_id=0) or []:
            text = f"{n.get('headline', '')} {n.get('summary', '')}"
            out += [t.strip().upper() for t in str(n.get("related") or "").split(",")]
            out += tickers_in_text(text)
            out += tickers_by_name(text, index)
    except Exception as e:
        logger.warning(f"market news unavailable: {e}")
    out = [t for t in dict.fromkeys(out) if t and t.isascii() and len(t) <= 6]
    if out and broker is not None:
        try:
            known = broker.get_assets_map(out)
            out = [t for t in out if t in known]
        except Exception as e:
            logger.warning(f"news tickers not validated ({e}); dropped")
            out = []
    return out


def snapshots(broker, symbols) -> dict:
    """{symbol: (latest price, previous close)} from Alpaca snapshots."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockSnapshotRequest
    out = {}
    symbols = list(symbols)
    for i in range(0, len(symbols), 200):
        chunk = symbols[i:i + 200]
        try:
            snaps = broker.data.get_stock_snapshot(
                StockSnapshotRequest(symbol_or_symbols=chunk, feed=DataFeed.IEX))
        except Exception as e:
            logger.warning(f"snapshots failed for {len(chunk)} names: {e}")
            continue
        for sym, s in (snaps or {}).items():
            trade = getattr(s, "latest_trade", None)
            prev = getattr(s, "previous_daily_bar", None)
            px = getattr(trade, "price", None) if trade else None
            pc = getattr(prev, "close", None) if prev else None
            if px and pc:
                out[sym] = (float(px), float(pc))
    return out


def tradable_only(broker, rows, config) -> list:
    """The universe's own asset rules: tradable, not OTC, ETFs skipped when
    the universe skips them. Unknown metadata keeps the name."""
    import universe
    if not rows:
        return rows
    try:
        assets = broker.get_assets_map([r["symbol"] for r in rows])
    except Exception as e:
        logger.warning(f"asset metadata unavailable: {e}")
        return rows
    skip_etfs = (config.get("universe") or {}).get("skip_etfs", True)
    keep = []
    for r in rows:
        a = assets.get(r["symbol"])
        if a is None:
            keep.append(r)
            continue
        if not a.get("tradable") or str(a.get("exchange", "")).upper().endswith("OTC"):
            continue
        if skip_etfs and universe.looks_like_etf(a.get("name", "")):
            continue
        r["name"] = a.get("name", "")
        keep.append(r)
    return keep


def company_news(client, symbol, since_date: str, to_date: str) -> list:
    try:
        return client.company_news(symbol, _from=since_date, to=to_date) or []
    except Exception as e:
        logger.warning(f"{symbol}: company news unavailable: {e}")
        return []


def candidate_pool(broker, client, config, origins: dict = None) -> list:
    """Pool symbols. `origins` (if given) is filled {symbol: set of 'pool' /
    'movers' / 'news'} so each scan can say which names ONLY Finnhub's market
    news surfaced - the evidence for (or against) a news-first scan."""
    import universe
    origins = origins if origins is not None else {}

    def add(syms, tag):
        for s in syms:
            if s:
                origins.setdefault(s, set()).add(tag)
    add(universe.scan_pool(config), "pool")
    try:
        add([m["symbol"] for m in broker.get_market_movers(top=50)], "movers")
    except Exception as e:
        logger.warning(f"market movers unavailable: {e}")
    if client is not None:
        add(market_news_tickers(client, broker), "news")
    return list(origins)


def news_only(origins: dict, symbols) -> list:
    """Of `symbols`, those that came ONLY from Finnhub market-news tickers
    (not in our scan pool, not an Alpaca mover)."""
    return [s for s in symbols if origins.get(s) == {"news"}]


def scan(broker=None, client=None, config=None, now=None, tagger=None) -> dict:
    """The 09:00 scan. Returns the catalysts_today payload."""
    config = config if config is not None else load_config()
    cfg = settings(config)
    now = now or datetime.now(ET)
    now_utc = now.astimezone(timezone.utc)
    if broker is None:
        from broker import Broker
        broker = Broker()
    if client is None:
        client = finnhub_client()
    tagger = tagger or (lambda h, t: tag_headline(h, t, config))

    origins = {}
    pool = candidate_pool(broker, client, config, origins)
    snaps = snapshots(broker, pool)
    gapped = []
    for sym, (px, pc) in snaps.items():
        g = gap_pct(px, pc)
        if g is not None and abs(g) >= cfg["min_gap_pct"] \
                and cfg["min_price"] <= px <= cfg["max_price"]:
            gapped.append({"symbol": sym, "price": px, "prev_close": pc,
                           "gap_pct": round(g, 2)})
    if gapped:
        try:
            daily = broker.get_daily_bars([r["symbol"] for r in gapped],
                                          lookback_days=40)
        except Exception as e:
            logger.warning(f"daily bars unavailable: {e}")
            daily = {}
        for r in gapped:
            r["adv_usd"] = avg_dollar_volume(daily.get(r["symbol"]))
    liquid = [r for r in gapped
              if r.get("adv_usd") and r["adv_usd"] >= cfg["min_dollar_volume"]]
    liquid = tradable_only(broker, liquid, config)
    liquid.sort(key=lambda r: -abs(r["gap_pct"]))
    since = (now - timedelta(days=4)).strftime("%Y-%m-%d")   # covers a weekend
    to = now.strftime("%Y-%m-%d")
    for r in liquid:
        if client is None:
            r["news"] = []
            continue
        r["news"] = recent_news(company_news(client, r["symbol"], since, to),
                                now_utc, cfg["news_hours"])
        if len([x for x in liquid if x.get("news")]) >= cfg["max_names"]:
            break
    picked = select(liquid, cfg)
    names = []
    for r in picked:
        top = best_headline(r["news"], r["symbol"], r.get("name", ""))
        names.append({
            "symbol": r["symbol"], "name": r.get("name", ""),
            "gap_pct": r["gap_pct"], "price": r["price"],
            "prev_close": r["prev_close"], "adv_usd": round(r["adv_usd"]),
            "headline": top.get("headline", "").strip(),
            "news_source": top.get("source"), "news_url": top.get("url"),
            "news_time": datetime.fromtimestamp(float(top["datetime"]), ET)
                         .strftime("%Y-%m-%d %H:%M"),
            "tag": tagger(top.get("headline", ""), r["symbol"]),
            "origin": sorted(origins.get(r["symbol"], ())),
        })
    found = [n["symbol"] for n in names]
    only_news = news_only(origins, found)
    logger.info(f"catalyst scan: {len(found)} names; {len(only_news)} only from "
                f"Finnhub market-news tickers {only_news}, "
                f"{len(found) - len(only_news)} from our pool/movers")
    return {"date": today_et(now), "generated_at": now.isoformat(),
            "pool": len(pool), "snapshots": len(snaps), "gapped": len(gapped),
            "pool_news_only": len(news_only(origins, pool)),
            "names_news_only": news_only(origins, [n["symbol"] for n in names]),
            "liquid": len(liquid), "finnhub": client is not None,
            "names": names}


# ============================================================ outputs

def write(payload, path=CATALYSTS_FILE):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def load_today(path=CATALYSTS_FILE, day: str = None) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return {}
    return payload if payload.get("date") == (day or today_et()) else {}


def journal_names(payload) -> list:
    """One decisions row per name, source='catalyst' (no approved flag: it is
    not a trade decision)."""
    import journal
    ids = []
    for n in payload.get("names", []):
        ctx = {k: n.get(k) for k in ("gap_pct", "price", "prev_close", "adv_usd",
                                     "tag", "headline", "news_time",
                                     "news_source")}
        ids.append(journal.log_decision(
            n["symbol"], "catalyst", ctx,
            {"catalyst": True, "tag": n.get("tag"),
             "reasoning": "Morning catalyst: added to the universe; no trade "
                          "from news alone."},
            source="catalyst"))
    return ids


def table_lines(payload) -> list:
    import trade_table
    w = trade_table.WIDTH
    names = payload.get("names", [])
    if not names:
        return ["No catalysts this morning (|gap| >= 3% with news in 18 h)."]
    lines = [f"{'TICK':<5} {'GAP%':>6} {'TAG':<9} HEADLINE", "-" * w]
    short = {"upgrade-downgrade": "up/down", "FDA-clinical": "FDA",
             "guidance": "guidance", "earnings": "earnings"}
    for n in names:
        head = f"{n['symbol'][:5]:<5} {n['gap_pct']:>+6.1f} " \
               f"{short.get(n['tag'], n['tag'])[:9]:<9} "
        text = n.get("headline", "")
        room = w - len(head)
        import textwrap
        chunks = textwrap.wrap(text, room)[:3] or [""]
        lines.append(head + chunks[0])
        lines += [" " * len(head) + c for c in chunks[1:]]   # wrapped under itself
    lines += ["-" * w, "In today's universe; no trade on news alone."]
    return lines


def post(payload) -> int:
    import alerts
    import trade_alerts
    import trade_table
    if not trade_alerts.enabled("catalysts"):
        return 0
    msgs = trade_table.to_messages(f"{TITLE} — {payload.get('date', '')}",
                                   table_lines(payload))
    for m in msgs:
        alerts.post_async(m)
    return len(msgs)


# ============================================================ main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="S36 morning catalyst scanner")
    ap.add_argument("--post", action="store_true",
                    help="post today's table (09:20 ET job)")
    ap.add_argument("--dry-run", action="store_true",
                    help="scan and print; write, journal and post nothing")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    from dotenv import load_dotenv
    load_dotenv()
    config = load_config()
    if not (config.get("catalysts") or {}).get("enabled", True):
        print("catalysts disabled in bot_config.json")
        return 0
    if a.post:
        import alerts
        payload = load_today()
        if not payload:
            payload = {"date": today_et(), "names": []}
        n = post(payload)
        alerts.drain()
        print(f"posted {n} message(s), {len(payload.get('names', []))} names")
        return 0
    payload = scan(config=config)
    print(json.dumps({k: v for k, v in payload.items() if k != "names"}))
    for line in table_lines(payload):
        print(line)
    if a.dry_run:
        return 0
    write(payload)
    import universe
    added = universe.add_catalysts_to_today(payload)
    ids = journal_names(payload)
    print(f"wrote {CATALYSTS_FILE}: {len(payload['names'])} names; "
          f"{added} added to today's universe file; journaled {len(ids)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
