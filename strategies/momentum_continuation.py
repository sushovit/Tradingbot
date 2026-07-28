"""
strategies/momentum_continuation.py — breakout with volume expansion.

Trigger (on the last CLOSED bar, the "breakout bar"):
  - close > prior 20-bar high (excluding the breakout bar itself)
  - volume > 1.5x its 20-bar average
  - bar-over-bar change > +3%
Entry is on the NEXT bar (current price). Stop goes below the breakout bar's
low — if price falls back through the bar that broke out, the momentum thesis
is invalid. Target is a minimum of 2R.
"""

from .base import Strategy, Signal, Rejection

VOLUME_MULT = 1.5
MIN_CHANGE_PCT = 3.0
LOOKBACK = 20
# Boardroom 2026-07-28: 3R floor. Backtest (3y daily): 3R targets beat 2R on
# expectancy for every strategy — momentum +0.32R vs +0.16R — at the cost of
# win rate (33% vs 39%).
MIN_TARGET_R = 3.0


class MomentumContinuation(Strategy):
    name = "momentum_continuation"
    timeframe = "daily"

    def detect(self, df, context: dict):
        ticker = context["ticker"]

        if df is None or len(df) < LOOKBACK + 2:
            return None

        df = df.copy()
        breakout_bar = df.iloc[-2]          # last closed bar
        prior_window = df.iloc[-(LOOKBACK + 2):-2]  # 20 bars before it

        prior_high = float(prior_window['high'].max())
        avg_volume = float(prior_window['volume'].mean())
        prev_close = float(df['close'].iloc[-3])
        change_pct = ((float(breakout_bar['close']) / prev_close) - 1) * 100 \
            if prev_close > 0 else 0.0

        broke_out = float(breakout_bar['close']) > prior_high
        if not broke_out:
            return None

        # --- Deterministic filters ---
        if avg_volume <= 0 or float(breakout_bar['volume']) <= avg_volume * VOLUME_MULT:
            return Rejection(self.name, ticker, "volume_low",
                             f"Breakout volume {breakout_bar['volume']:.0f} <= "
                             f"{VOLUME_MULT}x avg {avg_volume:.0f}")
        if change_pct <= MIN_CHANGE_PCT:
            return Rejection(self.name, ticker, "change_too_small",
                             f"Change {change_pct:.1f}% <= +{MIN_CHANGE_PCT}%")

        entry = float(df['close'].iloc[-1])          # next bar
        stop = float(breakout_bar['low'])            # thesis invalidation
        if stop >= entry:
            return Rejection(self.name, ticker, "invalid_stop",
                             "Breakout bar low is above current price")
        target = entry + (entry - stop) * MIN_TARGET_R

        return Signal(
            setup_name=self.name,
            ticker=ticker,
            entry=entry,
            stop=stop,
            target=target,
            confidence_hint="medium",
            reasoning=(f"Breakout close {breakout_bar['close']:.2f} > 20-bar high "
                       f"{prior_high:.2f} on {breakout_bar['volume'] / avg_volume:.1f}x "
                       f"volume, +{change_pct:.1f}%"),
            extras={"breakout_bar_low": stop, "prior_high": prior_high,
                    "rr_ratio": MIN_TARGET_R},
        )
