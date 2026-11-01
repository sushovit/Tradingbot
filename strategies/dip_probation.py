"""
strategies/dip_probation.py — the live dip probation setup (CEO-approved
owner override, 2026-11-01). The rule and its gates live in dip_live.py; the
worker adds the context it needs: daily bars, now_et, current_price and the
per-cycle dip_gate (max 1 open, max 1 entry a day, kill switch).
"""

from .base import Strategy


class DipProbation(Strategy):
    name = "dip_probation"
    timeframe = "intraday"

    def detect(self, df, context: dict):
        import dip_live
        return dip_live.detect(df, context)
