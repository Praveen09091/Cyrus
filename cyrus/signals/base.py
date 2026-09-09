"""Strategy contract.

A strategy turns bars into at most one proposal. It never sizes, never decides,
and never sends. It must attach a stop and an invalidation, or the proposal is
structurally faulty and dies before sizing (see Proposal.faults).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from cyrus.bus.messages import Evidence, Proposal, Side
from cyrus.indicators.core import Bar


@dataclass
class StrategyContext:
    """Everything a strategy is allowed to know."""

    book: str
    instrument: str
    timeframe: str
    bars: Sequence[Bar]
    params: Dict[str, object] = field(default_factory=dict)
    cycle_id: str = ""
    regime: str = "unknown"

    def param(self, name: str, default: object = None) -> object:
        value = self.params.get(name, default)
        if isinstance(value, dict):
            # Per-instrument overrides, e.g. entry_z: {SPY: 1.5, QQQ: 1.8}
            return value.get(self.instrument, default)
        return value

    def fparam(self, name: str, default: float) -> float:
        value = self.param(name, default)
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    def iparam(self, name: str, default: int) -> int:
        value = self.param(name, default)
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default


class Strategy:
    """Base class. Subclasses implement ``evaluate``."""

    name = "strategy"
    style = "generic"
    min_bars = 30

    def evaluate(self, ctx: StrategyContext) -> Optional[Proposal]:
        raise NotImplementedError

    def __call__(self, ctx: StrategyContext) -> Optional[Proposal]:
        if len(ctx.bars) < self.min_bars:
            return None
        proposal = self.evaluate(ctx)
        if proposal is None:
            return None
        if proposal.faults():
            # A malformed proposal is a strategy bug. Drop it rather than let
            # the risk kernel spend a cycle rejecting the same fault forever.
            return None
        return proposal

    def _proposal(
        self,
        ctx: StrategyContext,
        side: Side,
        entry: float,
        stop: float,
        target: float,
        thesis: str,
        bear_case: str,
        invalidation: str,
        indicators: Dict[str, float],
        sources: Optional[List[str]] = None,
    ) -> Proposal:
        return Proposal(
            sender="chartist",
            cycle_id=ctx.cycle_id,
            book=ctx.book,
            instrument=ctx.instrument,
            side=side,
            strategy=self.name,
            entry=entry,
            stop=stop,
            target=target,
            timeframe=ctx.timeframe,
            thesis=thesis,
            bear_case=bear_case,
            invalidation=invalidation,
            evidence=Evidence.SOURCED if sources else Evidence.MIXED,
            indicators=indicators,
            sources=sources or [],
        )


__all__ = ["Strategy", "StrategyContext"]
