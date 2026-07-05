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
    "max_candidates": 15,
    "skip_etfs": True,
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
# PURE FILTER/RANK (unit tested, no network)
# =============================================================================

def filter_and_rank(candidates: list, config: dict) -> list:
    """candidates: [{symbol, price, avg_dollar_volume, change_pct,
                     tradable, exchange, name}, ...]
    Returns the ranked, filtered list capped at max_candidates."""
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
        kept.append({
            "symbol": c["symbol"],
            "price": round(price, 2),
            "avg_dollar_volume": round(adv, 0),
            "change_pct": round(change, 2),
            "score": adv * abs(change),
            "name": c.get("name", ""),
        })
    kept.sort(key=lambda x: x["score"], reverse=True)
    return kept[: int(cfg["max_candidates"])]


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
        })
    return candidates


def refresh(broker, config: dict) -> list:
    """Fetch + filter + write universe_today.json. Returns the candidates."""
    ranked = filter_and_rank(fetch_candidates(broker, config), config)
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
    print("| # | Ticker | Price | Avg $ vol (20d) | Move % | Name |")
    print("|---|---|---|---|---|---|")
    for i, c in enumerate(ranked, 1):
        print(f"| {i} | {c['symbol']} | ${c['price']:,.2f} "
              f"| ${c['avg_dollar_volume'] / 1e6:,.0f}M | {c['change_pct']:+.1f}% "
              f"| {c['name'][:40]} |")
    print(f"\nWritten to {UNIVERSE_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
