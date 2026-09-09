"""Data plane: where bars come from, and how honest they are.

Two rules the desk never bends:
  1. Every feed declares whether it is realtime or delayed. Free data is
     delayed, and a strategy built on delayed data must not pretend otherwise.
  2. No feed ever invents a bar. A missing series returns empty and the cycle
     stands down, because a fabricated price is the fastest way to lose money.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

from cyrus.indicators.core import Bar


@dataclass
class FeedInfo:
    name: str
    provider: str
    realtime: bool
    note: str = ""

    @property
    def label(self) -> str:
        return "REALTIME" if self.realtime else "DELAYED"


class BarSource:
    """Interface for anything that can hand the desk OHLCV bars."""

    info: FeedInfo

    def fetch(self, instrument: str, timeframe: str, limit: int = 300) -> List[Bar]:
        raise NotImplementedError

    def dollar_volume(self, bars: Sequence[Bar], period: int = 20) -> Optional[float]:
        """Average dollar volume over the trailing window.

        Sentinel uses this as a liquidity floor. Returns None when volume is
        absent rather than guessing, so the kernel can skip the check honestly.
        """
        if len(bars) < period:
            return None
        window = bars[-period:]
        if all(b.volume <= 0 for b in window):
            return None
        return sum(b.close * b.volume for b in window) / period


class SourceRegistry:
    """Named feeds with a resolution order.

    Books ask for a feed by name. If it is unavailable, the registry falls
    back in declared order and records which feed actually answered, so the
    ledger shows the provenance of every bar the desk traded on.
    """

    def __init__(self) -> None:
        self._sources: Dict[str, BarSource] = {}
        self._order: List[str] = []
        self.last_used: Dict[str, str] = {}

    def register(self, name: str, source: BarSource, preferred: bool = False) -> None:
        self._sources[name] = source
        if preferred:
            self._order.insert(0, name)
        else:
            self._order.append(name)

    def get(self, name: str) -> Optional[BarSource]:
        return self._sources.get(name)

    def available(self) -> List[str]:
        return list(self._order)

    def fetch(
        self,
        instrument: str,
        timeframe: str,
        limit: int = 300,
        prefer: Optional[str] = None,
    ) -> List[Bar]:
        order = ([prefer] if prefer else []) + [n for n in self._order if n != prefer]
        for name in order:
            source = self._sources.get(name)
            if source is None:
                continue
            try:
                bars = source.fetch(instrument, timeframe, limit)
            except Exception:
                continue  # a dead feed is a fallback trigger, not a crash
            if bars:
                self.last_used[instrument] = name
                return bars
        self.last_used[instrument] = "none"
        return []

    def provenance(self, instrument: str) -> str:
        name = self.last_used.get(instrument, "none")
        source = self._sources.get(name)
        if source is None:
            return "none"
        return "%s (%s)" % (name, source.info.label)


def build_default_registry(allow_network: bool = True) -> SourceRegistry:
    """Yahoo first when it is installed, synthetic as the always-available floor.

    The synthetic feed is not a market simulator. It exists so the desk's
    plumbing, veto path, and journal can be exercised without a network, and
    it labels itself clearly so nobody mistakes its output for evidence.
    """
    from cyrus.data.synthetic import SyntheticSource

    registry = SourceRegistry()
    if allow_network:
        try:
            from cyrus.data.yahoo import YahooSource

            registry.register("yahoo", YahooSource(), preferred=True)
        except ImportError:
            pass
    registry.register("synthetic", SyntheticSource())
    return registry


TIMEFRAME_SECONDS: Dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


def timeframe_seconds(timeframe: str) -> int:
    if timeframe not in TIMEFRAME_SECONDS:
        raise KeyError("unsupported timeframe %r" % timeframe)
    return TIMEFRAME_SECONDS[timeframe]


def bars_per_year(timeframe: str) -> int:
    """Used to annualise volatility per book."""
    seconds = timeframe_seconds(timeframe)
    if timeframe == "1d":
        return 252
    trading_seconds_per_year = 252 * 6.5 * 3600
    return max(1, int(trading_seconds_per_year / seconds))


__all__ = [
    "FeedInfo",
    "BarSource",
    "SourceRegistry",
    "build_default_registry",
    "timeframe_seconds",
    "bars_per_year",
    "TIMEFRAME_SECONDS",
]
