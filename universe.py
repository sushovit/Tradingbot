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
import time
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
    "max_candidates": 20,        # DISPLAY cap (the table the CEO reads)
    "max_evaluated": 200,        # DETECTOR cap (what the loop actually scans)
    "skip_etfs": True,
    "core_watchlist": [],
    "pre_breakout_pct": 3.0,    # within 3% of the 20-day high
    "washout_pct": 10.0,        # >= 10% off the 20-day high (reclaim candidate)
}


# =============================================================================
# LIQUID SCAN POOL (Boardroom #2, item 5)
# =============================================================================
# Until now the scan saw only ~50 most-actives + ~50 movers + the 48-name core
# watchlist, so a setup forming in a name that wasn't moving YESTERDAY was
# invisible. This pool is the standing scan list: S&P 500 liquid leaders plus
# high-volume midcaps, all of which clear the $20M dollar-volume floor on a
# normal day. The floor is UNCHANGED — the pool widens what we look at, the
# filters still decide what qualifies.
LIQUID_POOL = [
    # Mega-cap tech / comms
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "AVGO", "TSLA",
    "NFLX", "ORCL", "CRM", "ADBE", "AMD", "INTC", "QCOM", "TXN", "MU", "AMAT",
    "LRCX", "KLAC", "ADI", "NXPI", "MRVL", "ON", "SWKS", "MCHP",
    "CSCO", "IBM", "ACN", "NOW", "INTU", "PANW", "CRWD", "SNOW", "DDOG", "NET",
    "ZS", "WDAY", "SHOP", "SQ", "PYPL", "COIN",
    "PLTR", "SNAP", "PINS", "RBLX", "UBER", "LYFT", "ABNB", "DASH", "TTD",
    "ROKU", "SPOT", "EA", "TTWO", "DIS", "WBD", "CMCSA", "T", "VZ",
    "TMUS", "DELL", "HPQ", "SMCI", "ANET", "FTNT",
    "APP", "ARM", "MSTR",
    # Financials
    "JPM", "BAC", "WFC", "C", "GS", "MS", "SCHW", "BLK", "AXP", "COF", "USB",
    "PNC", "TFC", "V", "MA", "SOFI", "HOOD", "ALLY",
    "BRK.B", "PGR",
    # Healthcare
    "UNH", "JNJ", "LLY", "PFE", "MRK", "ABBV", "BMY", "AMGN", "GILD",
    "VRTX", "REGN", "MRNA", "CVS", "CI", "HUM", "ISRG", "MDT", "TMO", "DHR",
    "ABT", "BSX", "SYK", "HIMS",
    # Consumer / retail
    "WMT", "COST", "TGT", "HD", "LOW", "NKE", "SBUX", "MCD", "CMG",
    "DKNG", "LULU", "TJX", "PEP", "KO", "PG",
    "MO", "PM", "STZ", "CCL", "RCL",
    "MAR", "BKNG", "F", "GM", "RIVN", "LCID",
    # Industrials / energy / materials
    "BA", "CAT", "DE", "GE", "HON", "MMM", "LMT", "RTX", "NOC", "UPS",
    "FDX", "UNP", "CSX", "DAL", "UAL", "AAL", "XOM", "CVX",
    "COP", "OXY", "SLB", "DVN", "FANG", "MPC", "PSX",
    "VLO", "KMI", "WMB", "OKE", "FCX", "NEM", "CLF", "X", "NUE", "AA",
    "LIN",
    # Utilities / REIT / other liquid
    "NEE", "DUK", "SO", "AEP", "PLD", "AMT",
]


def scan_pool(config: dict) -> list:
    """The full standing scan list: LIQUID_POOL plus any configured
    core_watchlist names not already in it, de-duplicated and order-stable."""
    cfg = _universe_config(config or {})
    return list(dict.fromkeys(
        list(LIQUID_POOL) + list(cfg.get("core_watchlist") or [])))


_ETF_NAME_MARKERS = ("ETF", "ETN", "TRUST", "FUND", "INDEX", "SHARES ")


def _universe_config(config: dict) -> dict:
    cfg = dict(DEFAULTS)
    cfg.update((config or {}).get("universe", {}))
    return cfg


def looks_like_etf(name: str) -> bool:
    upper = (name or "").upper()
    return any(marker in upper for marker in _ETF_NAME_MARKERS)


def prior_completed_close(df, today_str: str = None):
    """Close of the last COMPLETED session. During market hours the newest
    daily bar is today's partial bar — using it as the change base produces
    wrong moves; mixing it with screener changes produced the Goal 13 stale-
    price incident. Returns None if not derivable."""
    if df is None or len(df) == 0:
        return None
    today_str = today_str or datetime.now(EASTERN_TZ).strftime("%Y-%m-%d")
    try:
        last_bar_day = str(df.index[-1])[:10]
    except Exception:
        return float(df["close"].iloc[-1])
    if last_bar_day == today_str:
        return float(df["close"].iloc[-2]) if len(df) >= 2 else None
    return float(df["close"].iloc[-1])


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
    # Boardroom #2 item 5: detectors evaluate the FULL qualifying set
    # (max_evaluated); max_candidates is only how many rows we display.
    limit = int(cfg.get("max_evaluated") or cfg["max_candidates"])
    return kept[:limit]


def display_slice(ranked: list, config: dict) -> list:
    """Top-N rows for the human table. The scan is wide; the report is not."""
    cfg = _universe_config(config)
    return list(ranked)[: int(cfg["max_candidates"])]


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
    """Pull the standing liquid pool + most-actives + movers, enrich with
    bars/asset data. The pool is the floor of coverage: screener endpoints
    only surface what MOVED, so without it a setup coiling quietly in a
    liquid name is never even looked at."""
    symbols = {}
    for sym in scan_pool(config):
        symbols[sym] = {"symbol": sym, "change_pct": 0.0}
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
    live_prices = {}
    try:
        live_prices = broker.get_latest_prices(ticker_list)
    except Exception as e:
        logger.warning(f"live prices unavailable, falling back to bars: {e}")

    candidates = []
    for sym in ticker_list:
        df = bars.get(sym)
        if df is None or df.empty or len(df) < 2:
            continue
        # LIVE last trade is the price of record; bar close is only a fallback.
        last_price = live_prices.get(sym) or float(df["close"].iloc[-1])
        avg_dollar_volume = float((df["close"] * df["volume"]).tail(20).mean())
        prior = prior_completed_close(df)
        change_pct = symbols[sym].get("change_pct") or (
            float((last_price / prior - 1) * 100) if prior else 0.0)
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
    live_prices = {}
    try:
        live_prices = broker.get_latest_prices(watchlist)
    except Exception as e:
        logger.warning(f"live prices unavailable for watchlist: {e}")

    candidates = []
    for sym in watchlist:
        df = bars.get(sym)
        if df is None or df.empty or len(df) < 5:
            continue
        flag = classify_core_setup(df, config)
        if flag is None:
            continue
        asset = assets.get(sym, {})
        live = live_prices.get(sym) or float(df["close"].iloc[-1])
        prior = prior_completed_close(df)
        candidates.append({
            "symbol": sym,
            "price": live,
            "avg_dollar_volume": float((df["close"] * df["volume"]).tail(20).mean()),
            "change_pct": float((live / prior - 1) * 100) if prior else 0.0,
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
    t0 = time.time()
    movers = fetch_candidates(broker, config)
    try:
        core = fetch_core_candidates(broker, config)
    except Exception as e:
        logger.warning(f"core watchlist scan failed: {e}")
        core = []
    ranked = filter_and_rank(merge_candidates(movers, core), config)
    scan_seconds = round(time.time() - t0, 2)
    payload = {
        "generated_at": datetime.now(EASTERN_TZ).isoformat(),
        "date": datetime.now(EASTERN_TZ).strftime("%Y-%m-%d"),
        "scanned": len(movers) + len(core),
        "evaluated": len(ranked),
        "scan_seconds": scan_seconds,
        "candidates": ranked,
        "display": display_slice(ranked, config),
    }
    with open(UNIVERSE_FILE, "w") as f:
        json.dump(payload, f, indent=2)
    logger.info(f"Universe scan: {payload['scanned']} names fetched, "
                f"{payload['evaluated']} qualified, {scan_seconds}s")
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


def load_display_candidates() -> list:
    """The top-N rows meant for humans. Falls back to the head of the full
    list for universe files written before the expansion."""
    try:
        with open(UNIVERSE_FILE, "r") as f:
            payload = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return payload.get("display") or payload.get("candidates", [])[:20]


def last_scan_stats() -> dict:
    """{'scanned', 'evaluated', 'scan_seconds'} from the last refresh."""
    try:
        with open(UNIVERSE_FILE, "r") as f:
            payload = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {k: payload.get(k) for k in ("scanned", "evaluated", "scan_seconds")
            if payload.get(k) is not None}


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

    import clockline
    stats = last_scan_stats()
    shown = display_slice(ranked, config)
    print(f"# Universe — {stats.get('scanned', '?')} scanned, "
          f"{len(ranked)} qualified, top {len(shown)} shown "
          f"({stats.get('scan_seconds', '?')}s)")
    print(clockline.two_zone_line() + "\n")
    print("| # | Ticker | Price | Avg $ vol (20d) | Move % | Source | Setup | Name |")
    print("|---|---|---|---|---|---|---|---|")
    for i, c in enumerate(shown, 1):
        print(f"| {i} | {c['symbol']} | ${c['price']:,.2f} "
              f"| ${c['avg_dollar_volume'] / 1e6:,.0f}M | {c['change_pct']:+.1f}% "
              f"| {c.get('source', 'movers')} | {c.get('setup_flag') or '—'} "
              f"| {c['name'][:36]} |")
    print(f"\nAll {len(ranked)} qualifying names are scanned by the detectors; the table" + \
          f" above is the top {len(shown)} by score.")
    print(f"Written to {UNIVERSE_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
