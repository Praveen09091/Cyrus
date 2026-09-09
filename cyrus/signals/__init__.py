"""Signal generators, one per book style."""

from typing import Dict, Type

from cyrus.signals.base import Strategy, StrategyContext
from cyrus.signals.mean_reversion import MeanReversion
from cyrus.signals.momentum import MomentumBreakout
from cyrus.signals.trend import TrendFollow

REGISTRY: Dict[str, Type[Strategy]] = {
    "mean_reversion": MeanReversion,
    "momentum": MomentumBreakout,
    "trend": TrendFollow,
}


def for_style(style: str) -> Strategy:
    """Instantiate the strategy a book's style calls for."""
    if style not in REGISTRY:
        raise KeyError("no strategy registered for style %r" % style)
    return REGISTRY[style]()


__all__ = [
    "REGISTRY",
    "Strategy",
    "StrategyContext",
    "MeanReversion",
    "MomentumBreakout",
    "TrendFollow",
    "for_style",
]
