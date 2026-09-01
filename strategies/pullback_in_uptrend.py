"""
strategies/pullback_in_uptrend.py — buying rest in an established uptrend.

Boardroom-ratified live 2026-09-02 (work order item 3). Backtest evidence
(3y daily, 48 tickers): +0.23R trending / -0.14R chop at 3R, and 4R was the
data-optimal target (+0.35R, PF 1.45). The chop column is NEGATIVE, so the
SPY regime gate is enforced — this setup is never exempted from it.

Trigger (on the last CLOSED bar):
  - price above RISING 20- and 50-day EMAs (stacked AND sloping up)
  - within the last 12 bars, price pulled back and touched the 20-day EMA
  - that pullback came on DECLINING volume vs the 20 bars before it
  - the last closed bar is the FIRST close back above the prior day's high
    since the pullback low

Entry next bar. Stop under the pullback low — beneath it the pullback was a
reversal, not a rest. Target 4R.
"""

from .base import Strategy, Signal, Rejection

PULLBACK_LOOKBACK = 12
EMA_FAST = 20
EMA_SLOW = 50
SLOPE_BARS = 5           # EMAs must be higher than they were 5 bars ago
EMA_TOUCH_TOLERANCE = 1.01
TARGET_R = 4.0           # data-optimal per backtest_report.md


class PullbackInUptrend(Strategy):
    name = "pullback_in_uptrend"
    timeframe = "daily"

    def detect(self, df, context: dict):
        ticker = context["ticker"]
        if df is None or len(df) < EMA_SLOW + PULLBACK_LOOKBACK:
            return None

        hist = df.iloc[:-1]                 # completed bars only
        close = hist["close"]
        ema_fast = close.ewm(span=EMA_FAST, adjust=False).mean()
        ema_slow = close.ewm(span=EMA_SLOW, adjust=False).mean()
        bar, prior = hist.iloc[-1], hist.iloc[-2]
        fast = float(ema_fast.iloc[-1])
        slow = float(ema_slow.iloc[-1])

        # 1. an uptrend must be stacked AND rising; a flat 20>50 is not one.
        if fast <= slow:
            return None
        if not (fast > float(ema_fast.iloc[-1 - SLOPE_BARS])
                and slow > float(ema_slow.iloc[-1 - SLOPE_BARS])):
            return Rejection(self.name, ticker, "emas_not_rising",
                             f"EMA{EMA_FAST}/EMA{EMA_SLOW} not above their "
                             f"values {SLOPE_BARS} bars ago")
        if float(bar["close"]) <= fast:
            return None                     # not back above the 20 EMA yet

        # 2. a real pullback: price traded down to the 20-day EMA
        look = hist.iloc[-(PULLBACK_LOOKBACK + 1):-1]
        fast_look = ema_fast.iloc[-(PULLBACK_LOOKBACK + 1):-1]
        if len(look) < 4:
            return None
        if not bool((look["low"].values
                     <= fast_look.values * EMA_TOUCH_TOLERANCE).any()):
            return None                     # no pullback — nothing triggered

        # 3. the pullback must be ORDERLY: supply drying up, not distribution
        pull_vol = float(look["volume"].mean())
        base = hist["volume"].iloc[-(PULLBACK_LOOKBACK + 21):
                                   -(PULLBACK_LOOKBACK + 1)]
        base_vol = float(base.mean()) if len(base) else 0.0
        if base_vol <= 0 or pull_vol >= base_vol:
            return Rejection(self.name, ticker, "pullback_volume_not_declining",
                             f"Pullback volume {pull_vol:.0f} >= prior average "
                             f"{base_vol:.0f} — distribution, not rest")

        # 4. trigger: the FIRST close above the prior day's high SINCE the
        #    pullback low. Anchored to the pullback, because in a smooth
        #    grind-up every bar closes above the previous high.
        if float(bar["close"]) <= float(prior["high"]):
            return None
        lows = look["low"].values
        low_pos = int(lows.argmin())
        since = hist.iloc[-(PULLBACK_LOOKBACK + 1) + low_pos + 1:-1]
        if len(since) >= 2:
            prior_highs = since["high"].shift(1)
            if bool((since["close"].iloc[1:] > prior_highs.iloc[1:]).any()):
                return Rejection(self.name, ticker, "not_first_trigger",
                                 "An earlier bar since the pullback low "
                                 "already closed above its prior high")

        entry = float(df["close"].iloc[-1])
        stop = float(look["low"].min())
        if stop >= entry:
            return Rejection(self.name, ticker, "invalid_stop",
                             "Pullback low is above current price")
        target = entry + (entry - stop) * TARGET_R

        return Signal(
            setup_name=self.name,
            ticker=ticker,
            entry=entry,
            stop=stop,
            target=target,
            confidence_hint="medium",
            reasoning=(f"Pullback to the rising EMA{EMA_FAST} ({fast:.2f}) on "
                       f"{pull_vol / base_vol:.2f}x prior volume, then first "
                       f"close {float(bar['close']):.2f} back above the prior "
                       f"high {float(prior['high']):.2f}"),
            extras={"pullback_low": stop, "ema_fast": fast, "ema_slow": slow,
                    "pullback_volume_ratio": round(pull_vol / base_vol, 3),
                    "rr_ratio": TARGET_R},
        )
