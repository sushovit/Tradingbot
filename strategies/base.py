"""
strategies/base.py — pluggable strategy contract.

A Strategy looks at a ticker's OHLCV DataFrame (with indicators appended by
the caller or the strategy itself) and returns one of:

  Signal     — a fully-specified trade idea. Its stop MUST come from thesis
               invalidation (the level where the setup is proven wrong), never
               a fixed percentage.
  Rejection  — the setup's trigger fired but a DETERMINISTIC filter killed it
               (ADX low, volume low, ...). Rejections are journaled so the
               journal records every signal considered, taken or passed.
  None       — nothing to see; no trigger fired.

Every Signal flows through the same downstream pipeline:
AI gatekeeper -> risk validation -> broker bracket order -> journal.
No strategy may bypass it.
"""

from dataclasses import dataclass, field


@dataclass
class Signal:
    setup_name: str
    ticker: str
    entry: float
    stop: float
    target: float
    confidence_hint: str = "medium"   # low | medium | high
    reasoning: str = ""
    extras: dict = field(default_factory=dict)


@dataclass
class Rejection:
    setup_name: str
    ticker: str
    filter_name: str        # e.g. "adx_low", "volume_low", "rsi_low"
    details: str = ""


class Strategy:
    name = "base"
    timeframe = "intraday"

    def detect(self, df, context: dict):
        """Return Signal, Rejection, or None.

        df: OHLCV DataFrame, lowercase columns, oldest->newest.
        context: dict with at least ticker, risk_profile (dict), config (dict).
        """
        raise NotImplementedError
