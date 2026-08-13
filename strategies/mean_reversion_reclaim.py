"""
strategies/mean_reversion_reclaim.py — washed-out stock reclaiming its range.

Trigger (on the last CLOSED bar, the "reclaim bar"):
  - the ticker fell >= 10% from its 20-bar high at some point in the window
  - the reclaim bar closes back ABOVE the prior bar's high
  - AND above EMA9
  - on volume > 1.2x its 20-bar average
Stop goes below the reclaim bar's low — if price loses the level it just
reclaimed, the mean-reversion thesis is invalid. Target is the prior 20-bar
high (the level price is reverting toward), floored at 2R.
"""

import pandas_ta as ta

from .base import Strategy, Signal, Rejection

DRAWDOWN_PCT = 10.0
VOLUME_MULT = 1.2
LOOKBACK = 20
# Boardroom 2026-07-28: 3R floor (backtest: reclaim +0.38R at 3R vs +0.28R at
# 2R). The structural prior-high target still wins when it exceeds 3R.
MIN_TARGET_R = 3.0


class MeanReversionReclaim(Strategy):
    name = "mean_reversion_reclaim"
    timeframe = "daily"

    def detect(self, df, context: dict):
        ticker = context["ticker"]
        # Ratified 2026-08-13: threshold comes from config (1.3x), with the
        # module constant as the fallback the calibration study overrides.
        vol_mult = (context.get("config", {}) or {}).get(
            "volume_multipliers", {}).get(self.name, VOLUME_MULT)

        if df is None or len(df) < LOOKBACK + 2:
            return None

        df = df.copy()
        df['ema_9'] = ta.ema(df['close'], length=9)

        reclaim_bar = df.iloc[-2]           # last closed bar
        prior_bar = df.iloc[-3]
        window = df.iloc[-(LOOKBACK + 2):-2]

        high_20 = float(window['high'].max())
        low_after_high = float(window['low'].min())
        if high_20 <= 0:
            return None
        drawdown_pct = ((high_20 - low_after_high) / high_20) * 100

        washed_out = drawdown_pct >= DRAWDOWN_PCT
        reclaimed_prior_high = float(reclaim_bar['close']) > float(prior_bar['high'])
        if not (washed_out and reclaimed_prior_high):
            return None

        # --- Deterministic filters ---
        ema9 = reclaim_bar.get('ema_9')
        if ema9 is None or not float(reclaim_bar['close']) > float(ema9):
            return Rejection(self.name, ticker, "below_ema9",
                             "Reclaim bar closed below EMA9")
        avg_volume = float(window['volume'].mean())
        if avg_volume <= 0 or float(reclaim_bar['volume']) <= avg_volume * vol_mult:
            return Rejection(self.name, ticker, "volume_low",
                             f"Reclaim volume {reclaim_bar['volume']:.0f} <= "
                             f"{vol_mult}x avg {avg_volume:.0f}")

        # Playbook Rule #3 (gap-abort): a reclaim entry is invalid if the next
        # session opens below the reclaim bar's midpoint — the reclaim failed
        # overnight and buying the open is catching a falling knife.
        reclaim_mid = (float(reclaim_bar['high']) + float(reclaim_bar['low'])) / 2.0
        entry_bar_open = float(df['open'].iloc[-1])
        if entry_bar_open < reclaim_mid:
            return Rejection(self.name, ticker, "gap_below_reclaim_mid",
                             f"Open {entry_bar_open:.2f} < reclaim bar midpoint "
                             f"{reclaim_mid:.2f}")

        entry = float(df['close'].iloc[-1])
        stop = float(reclaim_bar['low'])     # thesis invalidation
        if stop >= entry:
            return Rejection(self.name, ticker, "invalid_stop",
                             "Reclaim bar low is above current price")
        # Revert toward the prior high, but never accept less than 2R.
        target = max(high_20, entry + (entry - stop) * MIN_TARGET_R)

        return Signal(
            setup_name=self.name,
            ticker=ticker,
            entry=entry,
            stop=stop,
            target=target,
            confidence_hint="medium",
            reasoning=(f"Reclaim after {drawdown_pct:.1f}% washout: close "
                       f"{reclaim_bar['close']:.2f} > prior high {prior_bar['high']:.2f} "
                       f"and EMA9, volume {reclaim_bar['volume'] / avg_volume:.1f}x avg"),
            extras={"reclaim_bar_low": stop, "high_20": high_20,
                    "drawdown_pct": drawdown_pct},
        )
