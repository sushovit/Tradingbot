"""
universe.py — daily tradable-universe scanner for a small account.

Replaces the fixed ticker list with a ranked candidate universe built from
Alpaca's screener (most-actives + top movers), filtered for what a $1,000
account can actually trade safely:

  - last price within [min_price, max_price] (default $5–$250: below $5 is
    penny-stock manipulation/spread territory; above $250 one share blows the
    per-position notional cap)
  - average daily dollar volume >= min_dollar_volume (default $20M: we must
    be able to enter and exit instantly at our size)
  - common stocks only: non-tradable and OTC symbols are skipped, ETFs
    optionally skipped per config

Candidates are ranked by dollar_volume x |% move| and capped at
max_candidates (default 15). Output goes to universe_today.json.

CLI:  python universe.py        -> prints the candidate table
Bot:  refreshed automatically once per session start; the loop scans
      universe tickers (plus any open positions) across all enabled
      strategies. Falls back to bot_config.json ticker_profiles when the
      universe is missing or stale.
"""

import json
import logging
import sys
from datetime import datetime

import pytz
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

EASTERN_TZ = pytz.timezone("US/Eastern")
UNIVERSE_FILE = "universe_today.json"
CONFIG_FILE = "bot_config.json"

DEFAULTS = {
    "min_price": 5.0,
    "max_price": 250.0,
    "min_dollar_volume": 20_000_000.0,
    "max_candidates": 20,
    "skip_etfs": True,
    "core_watchlist": [],
    "pre_breakout_pct": 3.0,    # within 3% of the 20-day high
    "washout_pct": 10.0,        # >= 10% off the 20-day high (reclaim candidate)
}

_ETF_NAME_MARKERS = ("ETF", "ETN", "TRUST", "FUND", "INDEX", "SHARES ")


def _universe_config(config: dict) -> dict:
    cfg = dict(DEFAULTS)
    cfg.update((config or {}).get("universe", {}))
    return cfg


def looks_like_etf(name: str) -> bool:
    upper = (name or "").upper()
    return any(marker in upper for marker in _ETF_NAME_MARKERS)


# =============================================================================
# PURE FILTER/RANK + SETUP CLASSIFICATION (unit tested, no network)
# =============================================================================

def classify_core_setup(df, config: dict = None):
    """Playbook-friendly state of a core-watchlist name from its daily bars:

      "pre_breakout"     — last close within pre_breakout_pct of the 20-day high
      "washout_reclaim"  — last close >= washout_pct below the 20-day high
      None               — mid-range, nothing actionable

    df: daily OHLCV, oldest->newest, needs >= 5 rows."""
    cfg = _universe_config(config or {})
    if df is None or len(df) < 5:
        return None
    window = df.tail(20)
    high20 = float(window["high"].max())
    last_close = float(df["close"].iloc[-1])
    if high20 <= 0:
        return None
    if last_close >= high20 * (1 - cfg["pre_breakout_pct"] / 100.0):
        return "pre_breakout"
    if last_close <= high20 * (1 - cfg["washout_pct"] / 100.0):
        return "washout_reclaim"
    return None


def filter_and_rank(candidates: list, config: dict) -> list:
    """candidates: [{symbol, price, avg_dollar_volume, change_pct,
                     tradable, exchange, name, source?, setup_flag?}, ...]
    Returns the ranked, filtered list capped at max_candidates.

    Score = avg dollar volume x |% move|. Core-watchlist names flagged on
    setup get a 1% move floor so a quiet pre-breakout coil isn't drowned out
    by yesterday's movers."""
    cfg = _universe_config(config)
    kept = []
    for c in candidates:
        price = float(c.get("price") or 0)
        adv = float(c.get("avg_dollar_volume") or 0)
        change = float(c.get("change_pct") or 0)
        if not c.get("tradable", True):
            continue
        if str(c.get("exchange", "")).upper().endswith("OTC"):
            continue
        if cfg["skip_etfs"] and looks_like_etf(c.get("name", "")):
            continue
        if not (cfg["min_price"] <= price <= cfg["max_price"]):
            continue
        if adv < cfg["min_dollar_volume"]:
            continue
        source = c.get("source", "movers")
        move_mult = abs(change)
        if source == "core_watch" and c.get("setup_flag"):
            move_mult = max(move_mult, 1.0)
        kept.append({
            "symbol": c["symbol"],
            "price": round(price, 2),
            "avg_dollar_volume": round(adv, 0),
            "change_pct": round(change, 2),
            "score": adv * move_mult,
            "source": source,
            "setup_flag": c.get("setup_flag"),
            "name": c.get("name", ""),
        })
    kept.sort(key=lambda x: x["score"], reverse=True)
    return kept[: int(cfg["max_candidates"])]


def merge_candidates(movers: list, core: list) -> list:
    """Union by symbol. A symbol in both keeps the movers entry (its screener
    change % is authoritative) but inherits the core setup_flag."""
    by_symbol = {}
    for c in movers:
        by_symbol[c["symbol"]] = dict(c, source=c.get("source", "movers"))
    for c in core:
        if c["symbol"] in by_symbol:
            if c.get("setup_flag") and not by_symbol[c["symbol"]].get("setup_flag"):
                by_symbol[c["symbol"]]["setup_flag"] = c["setup_flag"]
        else:
            by_symbol[c["symbol"]] = dict(c, source="core_watch")
    return list(by_symbol.values())


# =============================================================================
# FETCH (Alpaca screener + bars + assets)
# =============================================================================

def fetch_candidates(broker, config: dict) -> list:
    """Pull most-actives + movers, enrich with bars/asset data."""
    symbols = {}
    try:
        for a in broker.get_most_actives(top=50):
            symbols.setdefault(a["symbol"], {"symbol": a["symbol"], "change_pct": 0.0})
    except Exception as e:
        logger.warning(f"most-actives unavailable: {e}")
    try:
        for m in broker.get_market_movers(top=50):
            entry = symbols.setdefault(m["symbol"], {"symbol": m["symbol"]})
            entry["change_pct"] = m.get("percent_change", 0.0)
    except Exception as e:
        logger.warning(f"market movers unavailable: {e}")

    if not symbols:
        return []

    ticker_list = sorted(symbols.keys())
    assets = {}
    try:
        assets = broker.get_assets_map(ticker_list)
    except Exception as e:
        logger.warning(f"asset metadata unavailable: {e}")

    bars = broker.get_daily_bars(ticker_list, lookback_days=30)

    candidates = []
    for sym in ticker_list:
        df = bars.get(sym)
        if df is None or df.empty or len(df) < 2:
            continue
        closes = df["close"]
        last_price = float(closes.iloc[-1])
        avg_dollar_volume = float((df["close"] * df["volume"]).tail(20).mean())
        change_pct = symbols[sym].get("change_pct") or float(
            (closes.iloc[-1] / closes.iloc[-2] - 1) * 100)
        asset = assets.get(sym, {})
        candidates.append({
            "symbol": sym,
            "price": last_price,
            "avg_dollar_volume": avg_dollar_volume,
            "change_pct": change_pct,
            "tradable": asset.get("tradable", True),
            "exchange": asset.get("exchange", ""),
            "name": asset.get("name", ""),
            "source": "movers",
        })
    return candidates


def fetch_core_candidates(broker, config: dict) -> list:
    """Scan the static core watchlist for playbook setups: names coiling
    within 3% of their 20-day high (pre-breakout) or washed out >= 10% off
    highs (reclaim candidates). Only flagged names are returned."""
    cfg = _universe_config(config)
    watchlist = list(cfg.get("core_watchlist") or [])
    if not watchlist:
        return []

    assets = {}
    try:
        assets = broker.get_assets_map(watchlist)
    except Exception as e:
        logger.warning(f"asset metadata unavailable for core watchlist: {e}")
    bars = broker.get_daily_bars(watchlist, lookback_days=45)

    candidates = []
    for sym in watchlist:
        df = bars.get(sym)
        if df is None or df.empty or len(df) < 5:
            continue
        flag = classify_core_setup(df, config)
        if flag is None:
            continue
        closes = df["close"]
        asset = assets.get(sym, {})
        candidates.append({
            "symbol": sym,
            "price": float(closes.iloc[-1]),
            "avg_dollar_volume": float((df["close"] * df["volume"]).tail(20).mean()),
            "change_pct": float((closes.iloc[-1] / closes.iloc[-2] - 1) * 100)
            if len(closes) >= 2 else 0.0,
            "tradable": asset.get("tradable", True),
            "exchange": asset.get("exchange", ""),
            "name": asset.get("name", ""),
            "source": "core_watch",
            "setup_flag": flag,
        })
    return candidates


def refresh(broker, config: dict) -> list:
    """Fetch movers + core-watchlist setups, filter, rank, write
    universe_today.json. Returns the candidates."""
    movers = fetch_candidates(broker, config)
    try:
        core = fetch_core_candidates(broker, config)
    except Exception as e:
        logger.warning(f"core watchlist scan failed: {e}")
        core = []
    ranked = filter_and_rank(merge_candidates(movers, core), config)
    payload = {
        "generated_at": datetime.now(EASTERN_TZ).isoformat(),
        "date": datetime.now(EASTERN_TZ).strftime("%Y-%m-%d"),
        "candidates": ranked,
    }
    with open(UNIVERSE_FILE, "w") as f:
        json.dump(payload, f, indent=2)
    return ranked


def load_universe_tickers() -> list:
    """Symbols from universe_today.json if it was generated today (ET);
    otherwise [] so the caller falls back to the configured tickers."""
    try:
        with open(UNIVERSE_FILE, "r") as f:
            payload = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    if payload.get("date") != datetime.now(EASTERN_TZ).strftime("%Y-%m-%d"):
        return []
    return [c["symbol"] for c in payload.get("candidates", [])]


# =============================================================================
# CLI
# =============================================================================

def main():
    logging.basicConfig(level=logging.WARNING)
    try:
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        config = {}

    from broker import Broker
    try:
        broker = Broker()
    except Exception as e:
        print(f"Cannot connect to Alpaca paper account: {e}")
        return 1

    ranked = refresh(broker, config)
    if not ranked:
        print("No candidates passed the filters (screener empty or market data unavailable).")
        return 1

    print(f"# Universe — {datetime.now(EASTERN_TZ).strftime('%Y-%m-%d %H:%M ET')} "
          f"({len(ranked)} candidates)\n")
    print("| # | Ticker | Price | Avg $ vol (20d) | Move % | Source | Setup | Name |")
    print("|---|---|---|---|---|---|---|---|")
    for i, c in enumerate(ranked, 1):
        print(f"| {i} | {c['symbol']} | ${c['price']:,.2f} "
              f"| ${c['avg_dollar_volume'] / 1e6:,.0f}M | {c['change_pct']:+.1f}% "
              f"| {c.get('source', 'movers')} | {c.get('setup_flag') or '—'} "
              f"| {c['name'][:36]} |")
    print(f"\nWritten to {UNIVERSE_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
