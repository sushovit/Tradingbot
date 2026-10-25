"""
bars_store.py — CEO 2026-10-25: the PERMANENT 5-minute bar store.

The year of 5-minute IEX bars the studies replay on used to live in a
worktree's data/s33, and went when that worktree was removed in a disk
cleanup. It now lives in ONE place, the main checkout's data/bars_5m/
(m5_<SYM>.csv, the format every study loader reads), whichever worktree a
study runs from. It is never deleted by a cleanup: see disk_cleanup.KEEP.

    python bars_store.py --backfill           # fill/extend every symbol
    python bars_store.py --append             # nightly 17:00 ET: today's bars
    python bars_store.py --status             # files, bars, date range

Symbols: the scanner universe (universe_today.json candidates, which drift
day to day - every symbol ever stored is kept and extended), SPY, the sector
ETFs and the configured ticker_profiles. Network only through the Alpaca
data API (IEX, the live feed).
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

import pandas as pd

ET = "America/New_York"
START = "2025-08-15"          # S33/S34 window (2025-10-14) less 60 days of warm-up
ETFS = ("SPY", "QQQ", "IWM", "XLE", "XLK", "XLF", "XLV", "XLI", "XLY", "XLP",
        "XLU", "XLB", "XLRE", "XLC", "SMH")
COLS = ["open", "high", "low", "close", "volume"]


def repo_root(here: str = None) -> str:
    """The MAIN checkout, also when running from a git worktree (whose .git is
    a file 'gitdir: <main>/.git/worktrees/<name>')."""
    here = os.path.abspath(here or os.path.dirname(__file__))
    git = os.path.join(here, ".git")
    if os.path.isfile(git):
        with open(git, encoding="utf-8") as f:
            line = f.read().strip()
        if line.startswith("gitdir:"):
            gd = os.path.abspath(os.path.join(here, line.split(":", 1)[1].strip()))
            parts = gd.split(os.sep)
            if ".git" in parts:
                return os.sep.join(parts[:len(parts) - parts[::-1].index(".git") - 1])
    return here


BARS_DIR = os.environ.get("BARS_5M_DIR") or os.path.join(repo_root(), "data", "bars_5m")


def path_for(sym: str, store: str = None) -> str:
    return os.path.join(store or BARS_DIR, f"m5_{sym}.csv")


def stored_symbols(store: str = None) -> list:
    store = store or BARS_DIR
    if not os.path.isdir(store):
        return []
    return sorted(f[3:-4] for f in os.listdir(store)
                  if f.startswith("m5_") and f.endswith(".csv"))


def wanted_symbols(root: str = None, store: str = None) -> list:
    root = root or repo_root()
    syms = set(ETFS) | set(stored_symbols(store))
    try:
        with open(os.path.join(root, "universe_today.json"), encoding="utf-8") as f:
            syms |= {c["symbol"] for c in json.load(f).get("candidates", [])}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        with open(os.path.join(root, "bot_config.json"), encoding="utf-8") as f:
            syms |= set(json.load(f).get("ticker_profiles", {}))
    except (OSError, ValueError):
        pass
    return sorted(s for s in syms if s and "/" not in s)


def last_stamp(sym: str, store: str = None):
    """Timestamp of the last stored bar (UTC), or None."""
    p = path_for(sym, store)
    if not os.path.exists(p) or os.path.getsize(p) == 0:
        return None
    with open(p, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - 4096))
        tail = f.read().decode("utf-8", "replace").strip().splitlines()
    for line in reversed(tail):
        head = line.split(",", 1)[0]
        try:
            return pd.Timestamp(head).tz_convert("UTC") if pd.Timestamp(head).tzinfo \
                else pd.Timestamp(head).tz_localize("UTC")
        except (ValueError, TypeError):
            continue
    return None


def merge_write(sym: str, new: pd.DataFrame, store: str = None) -> int:
    """Append bars after the last stored one (atomic rewrite only on the
    first write; later writes append). Returns bars added."""
    store = store or BARS_DIR
    os.makedirs(store, exist_ok=True)
    if new is None or new.empty:
        return 0
    new = new[COLS].sort_index()
    new = new[~new.index.duplicated(keep="last")]
    last = last_stamp(sym, store)
    p = path_for(sym, store)
    if last is not None:
        new = new[new.index > last]
        if new.empty:
            return 0
        new.to_csv(p, mode="a", header=False)
    else:
        new.to_csv(p + ".tmp")
        os.replace(p + ".tmp", p)
    return len(new)


def fetch(data_client, sym: str, start, end) -> pd.DataFrame:
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    req = StockBarsRequest(symbol_or_symbols=sym,
                           timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                           start=start, end=end, feed=DataFeed.IEX)
    df = data_client.get_stock_bars(req).df
    if df is None or df.empty:
        return pd.DataFrame(columns=COLS)
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(sym, level="symbol")
    df.index = pd.DatetimeIndex(df.index).tz_convert("UTC")
    return df[COLS]


def update(symbols, start=START, end=None, store=None, data_client=None,
           log=print, pause_s=0.35) -> dict:
    """Extend every symbol from its last stored bar (or `start`) to `end`
    (default now). {'added': n, 'symbols': k, 'failed': [...]}"""
    if data_client is None:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(repo_root(), ".env"))
        from broker import Broker
        data_client = Broker().data
    def utc(x):
        x = pd.Timestamp(x)
        return x.tz_localize("UTC") if x.tzinfo is None else x.tz_convert("UTC")
    end = utc(end or datetime.utcnow().replace(microsecond=0))
    t0 = utc(start)
    out = {"added": 0, "symbols": 0, "failed": []}
    began = time.time()
    for i, sym in enumerate(symbols, 1):
        last = last_stamp(sym, store)
        s0 = (last + pd.Timedelta(minutes=5)) if last is not None else t0
        if s0 >= end:
            continue
        try:
            n = merge_write(sym, fetch(data_client, sym, s0.to_pydatetime(),
                                       end.to_pydatetime()), store)
        except Exception as e:
            out["failed"].append(sym)
            log(f"{sym}: fetch failed ({type(e).__name__}: {str(e)[:120]})")
            continue
        out["added"] += n
        out["symbols"] += 1
        if i % 20 == 0 or i == len(symbols):
            el = time.time() - began
            log(f"[{i}/{len(symbols)}] {sym} +{n} bars · {el / 60:.1f} min elapsed · "
                f"ETA {el / i * (len(symbols) - i) / 60:.1f} min")
        time.sleep(pause_s)                 # stay well under the 200/min limit
    return out


def status(store=None) -> list:
    syms = stored_symbols(store)
    size = sum(os.path.getsize(path_for(s, store)) for s in syms)
    lasts = [last_stamp(s, store) for s in syms]
    lasts = [x for x in lasts if x is not None]
    return [f"bars_5m: {len(syms)} symbols, {size / 1e6:,.0f} MB at {store or BARS_DIR}",
            f"last bar: {max(lasts).tz_convert(ET):%Y-%m-%d %H:%M} ET" if lasts else "empty"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--append", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args(argv)
    if a.backfill or a.append:
        syms = wanted_symbols() if a.backfill else (stored_symbols() or wanted_symbols())
        print(f"{'backfill' if a.backfill else 'append'}: {len(syms)} symbols -> {BARS_DIR}")
        r = update(syms)
        print(f"done: +{r['added']:,} bars over {r['symbols']} symbols; "
              f"failed {len(r['failed'])}: {', '.join(r['failed'][:20])}")
    print("\n".join(status()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
