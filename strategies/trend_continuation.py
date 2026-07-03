"""
strategies/trend_continuation.py — EMA golden cross + ADX + RSI + volume.

This is the original live_bot_worker entry logic MOVED here unchanged:
same indicators, same thresholds, same filter order. Stop is ATR-based below
the entry (approximates the swing low / slow EMA invalidation level), which
the playbook explicitly allows for this setup.
"""

import math

import pandas_ta as ta

from .base import Strategy, Signal, Rejection


class TrendContinuation(Strategy):
    name = "trend_continuation"
    timeframe = "intraday"

    def detect(self, df, context: dict):
        ticker = context["ticker"]
        rp = context["risk_profile"]
        cfg = context.get("config", {})

        fast_ema, slow_ema = rp["fast_ema"], rp["slow_ema"]
        adx_threshold = rp["adx_threshold"]
        rr_ratio, atr_multiplier = rp["rr_ratio"], rp["atr_multiplier"]
        use_volume_filter = rp.get("use_volume_filter", True)
        use_rsi_filter = cfg.get("use_rsi_filter", False)
        rsi_threshold = cfg.get("rsi_threshold", 50)
        use_confirmation_candle = cfg.get("use_confirmation_candle", False)

        df = df.copy()
        df['ema_fast'] = ta.ema(df['close'], length=fast_ema)
        df['ema_slow'] = ta.ema(df['close'], length=slow_ema)
        adx_df = ta.adx(df['high'], df['low'], df['close'], length=14)
        if adx_df is not None and not adx_df.empty:
            df['adx_14'] = adx_df['ADX_14']
        rsi_series = ta.rsi(df['close'], length=14)
        if rsi_series is not None and not rsi_series.empty:
            df['rsi_14'] = rsi_series
        atr_series = ta.atr(df['high'], df['low'], df['close'], length=14)
        if atr_series is not None and not atr_series.empty:
            df['atrr_14'] = atr_series
        if use_volume_filter:
            df['volume_ema_20'] = ta.ema(df['volume'], length=20)

        df.dropna(inplace=True)
        if len(df) < 3:
            return None

        last_closed = df.iloc[-2]
        prev_closed = df.iloc[-3]

        is_golden_cross = (last_closed['ema_fast'] > last_closed['ema_slow']
                           and prev_closed['ema_fast'] <= prev_closed['ema_slow'])
        if not is_golden_cross:
            return None

        # --- Deterministic filters (rejections are journaled by the caller) ---
        if 'adx_14' not in last_closed or last_closed['adx_14'] < adx_threshold:
            return Rejection(self.name, ticker, "adx_low",
                             f"ADX {last_closed.get('adx_14', float('nan')):.1f} < {adx_threshold}")
        if use_rsi_filter and ('rsi_14' not in last_closed
                               or last_closed['rsi_14'] < rsi_threshold):
            return Rejection(self.name, ticker, "rsi_low",
                             f"RSI {last_closed.get('rsi_14', float('nan')):.1f} < {rsi_threshold}")
        if use_confirmation_candle and not (df.iloc[-1]['ema_fast'] > df.iloc[-1]['ema_slow']):
            return Rejection(self.name, ticker, "no_confirmation",
                             "Confirmation candle did not hold above the cross")
        if use_volume_filter and ('volume_ema_20' not in last_closed
                                  or last_closed['volume'] < last_closed['volume_ema_20']):
            return Rejection(self.name, ticker, "volume_low",
                             "Signal volume below its 20-bar EMA")

        entry = float(df['close'].iloc[-1])
        atr_value = last_closed.get('atrr_14', float('nan'))
        if not isinstance(atr_value, (int, float)) or math.isnan(atr_value) or atr_value <= 0:
            return Rejection(self.name, ticker, "invalid_atr", "ATR unavailable")

        stop = entry - (atr_value * atr_multiplier)
        if entry - stop <= 0:
            return Rejection(self.name, ticker, "invalid_stop", "Stop >= entry")
        target = entry + ((entry - stop) * rr_ratio)

        return Signal(
            setup_name=self.name,
            ticker=ticker,
            entry=entry,
            stop=stop,
            target=target,
            confidence_hint="medium",
            reasoning=(f"EMA{fast_ema}>EMA{slow_ema} golden cross, "
                       f"ADX {last_closed.get('adx_14', 0):.1f}, "
                       f"RSI {last_closed.get('rsi_14', 0):.1f}"),
            extras={
                "df": df,
                "fast_ema": fast_ema,
                "slow_ema": slow_ema,
                "rr_ratio": rr_ratio,
            },
        )
