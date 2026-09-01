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
from .pullback_in_uptrend import PullbackInUptrend
from .post_earnings_continuation import PostEarningsContinuation

REGISTRY = {
    TrendContinuation.name: TrendContinuation,
    MomentumContinuation.name: MomentumContinuation,
    MeanReversionReclaim.name: MeanReversionReclaim,
    # Boardroom-ratified live 2026-09-02, on probation (half risk for the
    # first 20 live trades). Both are TRENDING-ONLY: neither may be added to
    # spy_filter_exempt — pullback_in_uptrend is -0.14R in chop.
    PullbackInUptrend.name: PullbackInUptrend,
    PostEarningsContinuation.name: PostEarningsContinuation,
}

DEFAULT_STRATEGIES = ["trend_continuation"]


def enabled_strategies(ticker: str, config: dict) -> list:
    """Instantiate the strategies enabled for a ticker.

    Per-ticker overrides come from bot_config.json "strategies"; tickers
    without an override (e.g. daily universe candidates) get the config's
    "default_strategies" list, falling back to the original single-strategy
    behaviour."""
    names = config.get("strategies", {}).get(ticker)
    if names is None:
        names = config.get("default_strategies", DEFAULT_STRATEGIES)
    instances = []
    for name in names:
        cls = REGISTRY.get(name)
        if cls is not None:
            instances.append(cls())
    return instances
