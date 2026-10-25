"""
bars_store.py — CEO 2026-10-25: the PERMANENT 5-minute bar store.

The year of 5-minute IEX bars the studies replay on used to live in a
worktree's data/s33, and went when that worktree was removed in a disk
cleanup. It now lives in ONE place, the main checkout's data/bars_5m/
(m5_<SYM>.parquet, zstd-compressed - CEO 2026-10-25; read with read()),
whichever worktree a study runs from. It is never deleted by a cleanup: see disk_cleanup.KEEP.

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


EXT = ".parquet"
COMPRESSION = "zstd"


def path_for(sym: str, store: str = None) -> str:
    return os.path.join(store or BARS_DIR, f"m5_{sym}{EXT}")


def stored_symbols(store: str = None) -> list:
    store = store or BARS_DIR
    if not os.path.isdir(store):
        return []
    return sorted({f[3:].rsplit(".", 1)[0] for f in os.listdir(store)
                   if f.startswith("m5_") and f.endswith((EXT, ".csv"))})


def read(sym: str, store: str = None):
    """All stored bars for `sym` (UTC index), or None. Parquet; a legacy
    m5_<SYM>.csv in the same folder is read if no parquet exists."""
    store = store or BARS_DIR
    p = path_for(sym, store)
    if os.path.exists(p):
        df = pd.read_parquet(p)
    elif os.path.exists(os.path.join(store, f"m5_{sym}.csv")):
        df = pd.read_csv(os.path.join(store, f"m5_{sym}.csv"), index_col=0)
    else:
        return None
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def _write(df: pd.DataFrame, path: str):
    df = df.copy()
    df.index.name = "timestamp"
    df.to_parquet(path + ".tmp", compression=COMPRESSION)
    os.replace(path + ".tmp", path)


def convert_csv(store: str = None, log=print) -> dict:
    """One-time migration: every m5_<SYM>.csv -> zstd parquet, verified
    (same rows, same last bar) before the CSV is removed."""
    store = store or BARS_DIR
    out = {"converted": 0, "csv_mb": 0.0, "parquet_mb": 0.0}
    for f in sorted(os.listdir(store)):
        if not (f.startswith("m5_") and f.endswith(".csv")):
            continue
        sym, csv = f[3:-4], os.path.join(store, f)
        df = pd.read_csv(csv, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        p = path_for(sym, store)
        _write(df, p)
        back = pd.read_parquet(p)
        if len(back) != len(df) or pd.Timestamp(back.index[-1]) != df.index[-1]:
            os.remove(p)
            log(f"{sym}: parquet check FAILED - csv kept")
            continue
        out["csv_mb"] += os.path.getsize(csv) / 1e6
        out["parquet_mb"] += os.path.getsize(p) / 1e6
        os.remove(csv)
        out["converted"] += 1
    return out


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
    df = read(sym, store)
    if df is None or df.empty:
        return None
    return pd.Timestamp(df.index.max())


def merge_write(sym: str, new: pd.DataFrame, store: str = None) -> int:
    """Add bars after the last stored one; the file is rewritten atomically
    (parquet cannot append). Returns bars added."""
    store = store or BARS_DIR
    os.makedirs(store, exist_ok=True)
    if new is None or new.empty:
        return 0
    new = new[COLS].sort_index()
    new.index = pd.to_datetime(new.index, utc=True)
    new = new[~new.index.duplicated(keep="last")]
    old = read(sym, store)
    if old is not None and not old.empty:
        new = new[new.index > old.index.max()]
        if new.empty:
            return 0
        merged = pd.concat([old[COLS], new])
    else:
        merged = new
    _write(merged, path_for(sym, store))
    legacy = os.path.join(store, f"m5_{sym}.csv")
    if os.path.exists(legacy):
        os.remove(legacy)               # its rows are now in the parquet
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
    ap.add_argument("--convert", action="store_true",
                    help="one-time: CSV files -> zstd parquet (verified)")
    a = ap.parse_args(argv)
    if a.convert:
        r = convert_csv()
        print(f"converted {r['converted']} files: {r['csv_mb']:,.0f} MB csv -> "
              f"{r['parquet_mb']:,.0f} MB parquet")
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
