"""
live_stream.py — S20 update (CEO 2026-10-23): live 1-minute candles for the
dashboard charts from Alpaca's IEX websocket. DISPLAY ONLY.

  * Runs in the DASHBOARD process only - constructing it inside the worker
    raises. Bot decisions stay on 5-minute bars; nothing here is read by
    the trading path.
  * One websocket (StockDataStream, feed IEX) in a daemon thread. Trades
    update the live 1-minute candle as they print; the stream's finished
    minute bar (and any updated bar) then replaces it. New symbols are
    subscribed on demand.
  * Backfill: today's 1-minute bars over REST when a symbol is first asked
    for. If the websocket cannot connect (the free plan allows ONE market-data
    connection), the dashboard falls back to polling 1-minute bars over REST
    on its 5-second refresh.
  * candles(symbol, "1m" | "5m" | "15m") -> [ts, open, high, low, close,
    volume] for today's regular session, ts naive ET.
"""

import logging
import threading
import time

import pandas as pd

import desk_config

logger = logging.getLogger("live_stream")
ET = "America/New_York"
TIMEFRAMES = {"1m": "1min", "5m": "5min", "15m": "15min"}
COLS = ("open", "high", "low", "close", "volume")


class LiveBars:
    def __init__(self, broker=None, stream_factory=None, clock=time.time):
        if desk_config.in_worker():
            raise RuntimeError("live_stream is dashboard-only: never in the worker")
        self._broker = broker
        self._factory = stream_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._bars = {}           # symbol -> {minute ts (UTC, tz-aware): [o,h,l,c,v]}
        self._symbols = set()
        self._stream = None
        self._thread = None
        self.streaming = False
        self.last_error = None
        self.last_event = None

    # ------------------------------------------------------------ ingest
    def on_trade(self, symbol, price, size, ts):
        """A trade print: update (or open) its minute's candle."""
        minute = pd.Timestamp(ts).tz_convert("UTC").floor("1min") \
            if pd.Timestamp(ts).tzinfo else pd.Timestamp(ts).tz_localize("UTC").floor("1min")
        price, size = float(price), float(size or 0)
        with self._lock:
            book = self._bars.setdefault(symbol, {})
            c = book.get(minute)
            if c is None:
                book[minute] = [price, price, price, price, size]
            else:
                c[1] = max(c[1], price)
                c[2] = min(c[2], price)
                c[3] = price
                c[4] += size
        self.last_event = self._clock()

    def on_bar(self, symbol, o, h, l, c, v, ts):
        """A finished (or updated) minute bar: authoritative, replaces it."""
        minute = pd.Timestamp(ts)
        minute = (minute.tz_convert("UTC") if minute.tzinfo
                  else minute.tz_localize("UTC")).floor("1min")
        with self._lock:
            self._bars.setdefault(symbol, {})[minute] = [float(o), float(h), float(l),
                                                         float(c), float(v or 0)]
        self.last_event = self._clock()

    def load_history(self, symbol, df):
        """REST 1-minute bars (index = timestamp): fill minutes not seen yet."""
        if df is None or getattr(df, "empty", True):
            return
        with self._lock:
            book = self._bars.setdefault(symbol, {})
            for ts, row in df.iterrows():
                m = pd.Timestamp(ts)
                m = (m.tz_convert("UTC") if m.tzinfo else m.tz_localize("UTC")).floor("1min")
                if m not in book:
                    book[m] = [float(row[k]) for k in COLS]

    # ------------------------------------------------------------ read
    def candles(self, symbol, timeframe="1m", session_date=None):
        rule = TIMEFRAMES.get(timeframe, "1min")
        with self._lock:
            book = dict(self._bars.get(symbol) or {})
        if not book:
            return pd.DataFrame(columns=["ts", *COLS])
        df = pd.DataFrame.from_dict(book, orient="index", columns=list(COLS)).sort_index()
        df.index = pd.DatetimeIndex(df.index).tz_convert(ET)
        mins = df.index.hour * 60 + df.index.minute
        day = session_date or df.index[-1].strftime("%Y-%m-%d")
        df = df[(df.index.strftime("%Y-%m-%d") == day) & (mins >= 570) & (mins < 960)]
        if rule != "1min" and not df.empty:
            df = df.resample(rule, label="left", closed="left").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last",
                 "volume": "sum"}).dropna(subset=["open"])
        out = df.reset_index().rename(columns={"index": "ts"})
        out.columns = ["ts", *COLS]
        out["ts"] = out["ts"].dt.tz_localize(None)
        return out

    # ------------------------------------------------------------ stream
    def ensure(self, symbols):
        """Backfill new symbols over REST and subscribe them on the stream."""
        new = [s for s in symbols if s not in self._symbols]
        if not new:
            return
        self._symbols.update(new)
        if self._broker is not None:
            try:
                hist = self._broker.get_bars(new, timeframe_minutes=1, lookback_days=1)
                for s in new:
                    self.load_history(s, hist.get(s))
            except Exception as e:
                logger.warning(f"1-minute backfill failed: {e}")
        if self._stream is not None:
            try:
                self._subscribe(new)
            except Exception as e:
                self.last_error = str(e)
        else:
            self._start()

    def poll(self, symbols):
        """REST fallback when the websocket is down: refresh 1-minute bars."""
        if self._broker is None or self.streaming:
            return
        try:
            hist = self._broker.get_bars(list(symbols), timeframe_minutes=1, lookback_days=1)
            for s in symbols:
                df = hist.get(s)
                if df is not None and not df.empty:
                    for ts, row in df.tail(3).iterrows():     # the last bars may still move
                        self.on_bar(s, *(row[k] for k in COLS), ts)
                    self.load_history(s, df)
        except Exception as e:
            self.last_error = str(e)

    def _subscribe(self, symbols):
        async def trade(t):
            self.on_trade(t.symbol, t.price, t.size, t.timestamp)

        async def bar(b):
            self.on_bar(b.symbol, b.open, b.high, b.low, b.close, b.volume, b.timestamp)
        self._stream.subscribe_trades(trade, *symbols)
        self._stream.subscribe_bars(bar, *symbols)
        self._stream.subscribe_updated_bars(bar, *symbols)

    def _start(self):
        if self._factory is None or (self._thread and self._thread.is_alive()):
            return
        try:
            self._stream = self._factory()
            self._subscribe(sorted(self._symbols))
        except Exception as e:
            self.last_error = str(e)
            self._stream = None
            return

        def run():
            backoff = 5
            while True:
                try:
                    self.streaming = True
                    self._stream.run()            # blocks; returns/raises on drop
                except Exception as e:
                    self.last_error = str(e)
                    logger.warning(f"IEX stream dropped: {e}")
                self.streaming = False
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
        self._thread = threading.Thread(target=run, name="iex-stream", daemon=True)
        self._thread.start()


def default_factory():
    import os
    from alpaca.data.enums import DataFeed
    from alpaca.data.live import StockDataStream
    return StockDataStream(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"),
                           feed=DataFeed.IEX)
