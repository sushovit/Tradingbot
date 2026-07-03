"""
strategies — pluggable setup detectors.

Auto-detected strategies live here. Event/flow setups (index inclusions,
scheduled catalysts) CANNOT be auto-detected and enter ONLY through orders.py
order sheets with setup="event_flow" and a required hard_exit_date.

Per-ticker enablement comes from bot_config.json, e.g.:
    "strategies": {"NVDA": ["trend_continuation", "momentum_continuation"]}
Tickers with no entry fall back to ["trend_continuation"] (the original
behaviour before the playbook refactor).
"""

from .base import Strategy, Signal, Rejection
from .trend_continuation import TrendContinuation
from .momentum_continuation import MomentumContinuation
from .mean_reversion_reclaim import MeanReversionReclaim

REGISTRY = {
    TrendContinuation.name: TrendContinuation,
    MomentumContinuation.name: MomentumContinuation,
    MeanReversionReclaim.name: MeanReversionReclaim,
}

DEFAULT_STRATEGIES = ["trend_continuation"]


def enabled_strategies(ticker: str, config: dict) -> list:
    """Instantiate the strategies enabled for a ticker in bot_config.json."""
    names = config.get("strategies", {}).get(ticker, DEFAULT_STRATEGIES)
    instances = []
    for name in names:
        cls = REGISTRY.get(name)
        if cls is not None:
            instances.append(cls())
    return instances
