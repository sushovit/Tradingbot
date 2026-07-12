"""
broker.py — Alpaca PAPER trading wrapper.

PAPER TRADING ONLY. The paper endpoint is hardcoded (paper=True) and this
module refuses to start if the environment is configured to point at a live
endpoint. Do not modify this to trade live.

All calls retry with exponential backoff on transient failures and raise
BrokerError when the broker is unreachable — callers must catch BrokerError
and skip the cycle rather than crash the loop.

Market data uses Alpaca's free IEX feed.
"""

import os
import time
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    TakeProfitRequest,
    StopLossRequest,
    GetOrdersRequest,
    GetOrderByIdRequest,
    ReplaceOrderRequest,
)
from alpaca.trading.enums import (OrderSide, TimeInForce, OrderClass,
                                  QueryOrderStatus, AssetStatus, AssetClass)
from alpaca.trading.requests import GetAssetsRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.requests import (StockBarsRequest, MostActivesRequest,
                                  MarketMoversRequest)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed, MostActivesBy, MarketType

load_dotenv()
logger = logging.getLogger(__name__)

PAPER_ENDPOINT = "https://paper-api.alpaca.markets"


class BrokerError(Exception):
    """Raised after all retries are exhausted. Callers skip the cycle."""


def _assert_paper_only():
    """Refuse to start if any env var points at a live Alpaca endpoint."""
    for var in ("ALPACA_BASE_URL", "APCA_API_BASE_URL"):
        url = os.getenv(var, "").strip()
        if url and "paper-api" not in url:
            raise RuntimeError(
                f"{var} is set to '{url}', which is not the paper endpoint. "
                "This bot is PAPER TRADING ONLY — refusing to start."
            )


def _retry(fn, *args, attempts: int = 3, what: str = "broker call", **kwargs):
    """Run fn with retries (1s/2s/4s backoff). Raises BrokerError at the end."""
    last_err = None
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_err = e
            if attempt < attempts - 1:
                wait = 2 ** attempt
                logger.warning(f"{what} failed (attempt {attempt + 1}/{attempts}), "
                               f"retrying in {wait}s: {e}")
                time.sleep(wait)
    raise BrokerError(f"{what} failed after {attempts} attempts: {last_err}")


class Broker:
    def __init__(self, api_key: str = None, secret_key: str = None):
        _assert_paper_only()
        api_key = api_key or os.getenv("ALPACA_API_KEY")
        secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            raise RuntimeError(
                "ALPACA_API_KEY / ALPACA_SECRET_KEY not set in .env — "
                "create paper-trading keys at https://app.alpaca.markets (Paper account)."
            )
        # paper=True is deliberate and non-configurable.
        self.trading = TradingClient(api_key, secret_key, paper=True)
        self.data = StockHistoricalDataClient(api_key, secret_key)
        self.screener = ScreenerClient(api_key, secret_key)

    # ---------------- Account & positions ----------------

    def get_account(self):
        return _retry(self.trading.get_account, what="get_account")

    def get_equity(self) -> float:
        return float(self.get_account().equity)

    def get_positions(self) -> list:
        return _retry(self.trading.get_all_positions, what="get_positions")

    def get_position(self, ticker: str):
        """Returns the position or None if flat."""
        try:
            return _retry(self.trading.get_open_position, ticker,
                          attempts=1, what=f"get_position({ticker})")
        except BrokerError as e:
            if "position does not exist" in str(e).lower() or "404" in str(e):
                return None
            raise

    # ---------------- Orders ----------------

    def get_open_orders(self, ticker: str = None) -> list:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN,
                               symbols=[ticker] if ticker else None)
        return _retry(self.trading.get_orders, req, what="get_open_orders")

    def get_order(self, order_id: str):
        # nested=True so bracket legs are included in the response.
        return _retry(self.trading.get_order_by_id, order_id,
                      GetOrderByIdRequest(nested=True),
                      what=f"get_order({order_id})")

    # Order states that are still working at the broker. "held" is what
    # Alpaca uses for bracket exit legs waiting on the parent/sibling —
    # status=open queries do NOT return them, so reports must use this.
    LIVE_ORDER_STATES = {"new", "accepted", "held", "partially_filled",
                         "pending_new", "pending_replace", "accepted_for_bidding"}

    def get_live_orders(self, ticker: str = None) -> list:
        """Every order still working at the broker, INCLUDING held bracket
        legs (stop/target waiting on the parent fill)."""
        req = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500,
                               symbols=[ticker] if ticker else None)
        orders = _retry(self.trading.get_orders, req, what="get_live_orders")
        live = []
        for o in orders:
            status = str(getattr(o, "status", "")).lower().split(".")[-1]
            if status in self.LIVE_ORDER_STATES:
                live.append(o)
        return live

    def get_closed_orders_since(self, since_utc: datetime) -> list:
        """Closed orders with a fill after `since_utc` (for exit sync)."""
        req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=since_utc,
                               limit=500)
        orders = _retry(self.trading.get_orders, req,
                        what="get_closed_orders_since")
        return [o for o in orders if o.filled_at is not None]

    def get_orders_filled_today(self) -> list:
        start_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                                       microsecond=0)
        req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=start_utc,
                               limit=200)
        orders = _retry(self.trading.get_orders, req, what="get_orders_filled_today")
        return [o for o in orders if o.filled_at is not None]

    def submit_bracket(self, ticker: str, qty: int, stop_price: float,
                       target_price: float):
        """Market BUY with attached stop-loss and take-profit legs.
        The exit orders live AT THE BROKER, not in our polling loop."""
        req = MarketOrderRequest(
            symbol=ticker,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=round(target_price, 2)),
            stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
        )
        return _retry(self.trading.submit_order, req,
                      what=f"submit_bracket({ticker})")

    def replace_stop(self, order_id: str, new_stop_price: float):
        """Ratchet a bracket's stop leg up (trailing-stop behaviour)."""
        req = ReplaceOrderRequest(stop_price=round(new_stop_price, 2))
        return _retry(self.trading.replace_order_by_id, order_id, req,
                      what=f"replace_stop({order_id})")

    def cancel_order(self, order_id: str):
        return _retry(self.trading.cancel_order_by_id, order_id,
                      what=f"cancel_order({order_id})")

    def close_position(self, ticker: str):
        """Cancel any open orders on the symbol, then market-close the position."""
        try:
            for order in self.get_open_orders(ticker):
                try:
                    self.trading.cancel_order_by_id(order.id)
                except Exception as e:
                    logger.warning(f"Could not cancel order {order.id} on {ticker}: {e}")
        except BrokerError as e:
            logger.warning(f"Could not list open orders for {ticker}: {e}")
        return _retry(self.trading.close_position, ticker,
                      what=f"close_position({ticker})")

    # ---------------- Screener (market data v1beta1) ----------------

    def get_most_actives(self, top: int = 50) -> list:
        """Most-active symbols by share volume: [{'symbol', 'volume'}, ...]"""
        req = MostActivesRequest(by=MostActivesBy.VOLUME, top=top)
        result = _retry(self.screener.get_most_actives, req, what="get_most_actives")
        return [{"symbol": a.symbol, "volume": float(getattr(a, "volume", 0) or 0)}
                for a in (getattr(result, "most_actives", None) or [])]

    def get_market_movers(self, top: int = 50) -> list:
        """Top gainers + losers: [{'symbol', 'percent_change', 'price'}, ...]"""
        req = MarketMoversRequest(market_type=MarketType.STOCKS, top=top)
        result = _retry(self.screener.get_market_movers, req, what="get_market_movers")
        movers = []
        for m in list(getattr(result, "gainers", None) or []) + \
                 list(getattr(result, "losers", None) or []):
            movers.append({"symbol": m.symbol,
                           "percent_change": float(getattr(m, "percent_change", 0) or 0),
                           "price": float(getattr(m, "price", 0) or 0)})
        return movers

    def get_assets_map(self, symbols) -> dict:
        """{symbol: {'tradable', 'exchange', 'name'}} for the given symbols.
        One bulk call — used to skip OTC / non-tradable / (optionally) ETFs."""
        wanted = set(symbols)
        req = GetAssetsRequest(status=AssetStatus.ACTIVE,
                               asset_class=AssetClass.US_EQUITY)
        assets = _retry(self.trading.get_all_assets, req, what="get_assets_map")
        out = {}
        for a in assets:
            if a.symbol in wanted:
                out[a.symbol] = {
                    "tradable": bool(getattr(a, "tradable", False)),
                    "exchange": str(getattr(a, "exchange", "") or ""),
                    "name": str(getattr(a, "name", "") or ""),
                }
        return out

    # ---------------- Market data (IEX feed) ----------------

    def get_bars(self, tickers, timeframe_minutes: int = 5,
                 lookback_days: int = 5) -> dict:
        """Intraday OHLCV bars from the free IEX feed.

        Returns {ticker: DataFrame} with lowercase open/high/low/close/volume
        columns indexed by timestamp. Missing tickers map to empty DataFrames.
        Replaces yfinance.
        """
        if isinstance(tickers, str):
            tickers = [tickers]
        start = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        req = StockBarsRequest(
            symbol_or_symbols=list(tickers),
            timeframe=TimeFrame(timeframe_minutes, TimeFrameUnit.Minute),
            start=start,
            feed=DataFeed.IEX,
        )
        barset = _retry(self.data.get_stock_bars, req, what="get_bars")
        result = {}
        df_all = barset.df if hasattr(barset, "df") else pd.DataFrame()
        for ticker in tickers:
            try:
                if df_all.empty:
                    result[ticker] = pd.DataFrame()
                    continue
                if isinstance(df_all.index, pd.MultiIndex):
                    if ticker not in df_all.index.get_level_values("symbol"):
                        result[ticker] = pd.DataFrame()
                        continue
                    df = df_all.xs(ticker, level="symbol").copy()
                else:
                    df = df_all.copy()
                df.columns = [c.lower() for c in df.columns]
                keep = [c for c in ("open", "high", "low", "close", "volume")
                        if c in df.columns]
                result[ticker] = df[keep]
            except Exception as e:
                logger.warning(f"Could not extract bars for {ticker}: {e}")
                result[ticker] = pd.DataFrame()
        return result

    def get_daily_bars(self, tickers, lookback_days: int = 60) -> dict:
        """Daily OHLCV bars (IEX feed) for the daily-timeframe strategies."""
        if isinstance(tickers, str):
            tickers = [tickers]
        start = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        req = StockBarsRequest(
            symbol_or_symbols=list(tickers),
            timeframe=TimeFrame.Day,
            start=start,
            feed=DataFeed.IEX,
        )
        barset = _retry(self.data.get_stock_bars, req, what="get_daily_bars")
        result = {}
        df_all = barset.df if hasattr(barset, "df") else pd.DataFrame()
        for ticker in tickers:
            try:
                if df_all.empty:
                    result[ticker] = pd.DataFrame()
                    continue
                if isinstance(df_all.index, pd.MultiIndex):
                    if ticker not in df_all.index.get_level_values("symbol"):
                        result[ticker] = pd.DataFrame()
                        continue
                    df = df_all.xs(ticker, level="symbol").copy()
                else:
                    df = df_all.copy()
                df.columns = [c.lower() for c in df.columns]
                keep = [c for c in ("open", "high", "low", "close", "volume")
                        if c in df.columns]
                result[ticker] = df[keep]
            except Exception as e:
                logger.warning(f"Could not extract daily bars for {ticker}: {e}")
                result[ticker] = pd.DataFrame()
        return result

    def get_latest_price(self, ticker: str) -> float:
        bars = self.get_bars([ticker], timeframe_minutes=1, lookback_days=1)
        df = bars.get(ticker)
        if df is None or df.empty:
            raise BrokerError(f"No recent price data for {ticker}")
        return float(df["close"].iloc[-1])
