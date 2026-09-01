"""
strategies/post_earnings_continuation.py — post-earnings drift continuation.

Boardroom-ratified live 2026-09-02 (work order item 2). Backtest evidence
(3y daily, 48 tickers): +0.38R trending / +0.96R chop at 3R, and 4R was the
data-optimal target (+0.61R, PF 2.08). Chop is 14 trades, which is why the
live setup is TRENDING ONLY — the SPY gate is not exempted.

Trigger (on the last CLOSED bar):
  - a gap-up >= 5% on >= 2x its 20-bar average volume within the last 5
    sessions ("the gap day")
  - an ACTUAL earnings event within 3 sessions of that gap day
  - the last closed bar is the FIRST close above the gap day's high

Entry next bar. Stop under the gap day's low — below it, the market has
given the whole earnings reaction back and the drift thesis is dead.
Target 4R. The position carries a 55-session hold cap so it can never span
the next print.

The earnings gate FAILS CLOSED: if the calendar cannot be read the setup is
rejected as 'earnings_calendar_unavailable', never taken on the gap alone.
Without it this is the generic gap-up class (M&A pops, guidance raises),
which is NOT what the boardroom ratified.
"""

import datetime
import logging

import earnings as earnings_calendar

from .base import Strategy, Signal, Rejection

GAP_PCT = 5.0
GAP_VOL_MULT = 2.0
GAP_WINDOW = 5           # trigger must arrive within 5 sessions of the gap
EARNINGS_SESSIONS = 3    # the print must be within 3 sessions of the gap day
MAX_HOLD_SESSIONS = 55   # < one quarter: never spans the next print
TARGET_R = 4.0           # data-optimal per backtest_report.md
LOOKBACK = 20

logger = logging.getLogger(__name__)


def _bar_date(df, pos):
    """Calendar date of a bar, or None if the index is not datetime-like."""
    try:
        return df.index[pos].date()
    except (AttributeError, IndexError):
        return None


class PostEarningsContinuation(Strategy):
    name = "post_earnings_continuation"
    timeframe = "daily"

    def detect(self, df, context: dict):
        ticker = context["ticker"]
        if df is None or len(df) < LOOKBACK + GAP_WINDOW + 4:
            return None

        hist = df.iloc[:-1]            # completed bars; df[-1] is forming
        bar = hist.iloc[-1]            # the trigger bar (last closed)

        for back in range(1, GAP_WINDOW + 1):
            if len(hist) < LOOKBACK + back + 3:
                break
            gap_bar = hist.iloc[-1 - back]
            prev = hist.iloc[-2 - back]
            prev_close = float(prev["close"])
            if prev_close <= 0:
                continue
            gap_pct = (float(gap_bar["open"]) / prev_close - 1) * 100
            if gap_pct < GAP_PCT:
                continue
            base = hist["volume"].iloc[-LOOKBACK - 1 - back:-1 - back]
            base_vol = float(base.mean()) if len(base) else 0.0
            if base_vol <= 0 or float(gap_bar["volume"]) < base_vol * GAP_VOL_MULT:
                continue

            gap_high, gap_low = float(gap_bar["high"]), float(gap_bar["low"])

            # The trigger: FIRST close above the gap day's high.
            if float(bar["close"]) <= gap_high:
                return None
            between = hist.iloc[-back:]
            if len(between) > 1 and bool(
                    (between["close"].iloc[:-1] > gap_high).any()):
                return Rejection(self.name, ticker, "not_first_close_above",
                                 f"An earlier bar already closed above the gap "
                                 f"high {gap_high:.2f}")

            # --- Earnings gate: this is what makes it the earnings class ---
            gap_day = _bar_date(hist, -1 - back) or datetime.date.today()
            verdict = earnings_calendar.had_earnings_within(
                ticker, EARNINGS_SESSIONS, asof=gap_day)
            if verdict is None:
                return Rejection(
                    self.name, ticker, "earnings_calendar_unavailable",
                    f"Could not confirm an earnings event near {gap_day} — "
                    f"failing closed rather than trading a bare gap")
            if verdict is False:
                return Rejection(
                    self.name, ticker, "no_earnings_event",
                    f"{gap_pct:+.1f}% gap on {gap_day} with no earnings event "
                    f"within {EARNINGS_SESSIONS} sessions — generic gap, not "
                    f"the ratified setup")

            entry = float(df["close"].iloc[-1])
            stop = gap_low
            if stop >= entry:
                return Rejection(self.name, ticker, "invalid_stop",
                                 "Gap-day low is above current price")
            target = entry + (entry - stop) * TARGET_R

            return Signal(
                setup_name=self.name,
                ticker=ticker,
                entry=entry,
                stop=stop,
                target=target,
                confidence_hint="medium",
                reasoning=(f"Earnings gap {gap_pct:+.1f}% on {gap_day} at "
                           f"{float(gap_bar['volume']) / base_vol:.1f}x volume; "
                           f"first close {float(bar['close']):.2f} above the "
                           f"gap high {gap_high:.2f} ({back} session(s) later)"),
                extras={"gap_date": str(gap_day), "gap_high": gap_high,
                        "gap_low": gap_low, "gap_pct": round(gap_pct, 2),
                        "sessions_since_gap": back,
                        "max_hold_sessions": MAX_HOLD_SESSIONS,
                        "rr_ratio": TARGET_R},
            )
        return None
