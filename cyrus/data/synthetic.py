"""Synthetic bars for exercising the desk without a network.

This is plumbing, not a market model. It produces a deterministic series with
a trend, a cycle, and noise so that indicators warm up, strategies fire, and
the veto path can be tested end to end.

It labels itself SYNTHETIC everywhere. Any proposal built on it is a wiring
test, never evidence, and Oracle refuses to grant it an out-of-sample edge.
"""

from __future__ import annotations

import math
import random
import time
from typing import List

from cyrus.data.sources import BarSource, FeedInfo, timeframe_seconds
from cyrus.indicators.core import Bar

_PROFILES = {
    # instrument: (base_price, annual_drift, daily_vol, cycle_period_bars, cycle_amp_pct)
    # ^VIX needs its own profile: falling back to the generic base of 100 would
    # read as a permanent volatility crisis and pin Atlas to a stress regime.
    "^VIX": (15.0, 0.0, 0.030, 60, 3.0),
    "DX-Y.NYB": (104.0, 0.0, 0.004, 110, 0.3),
    "^TNX": (4.2, 0.0, 0.010, 90, 0.8),
    "SPY": (560.0, 0.08, 0.010, 96, 0.6),
    "QQQ": (480.0, 0.12, 0.014, 96, 0.9),
    "GLD": (250.0, 0.06, 0.009, 120, 0.5),
    "USO": (78.0, 0.02, 0.018, 140, 1.2),
    "BTC-USD": (95000.0, 0.30, 0.035, 72, 2.0),
    "ETH-USD": (3400.0, 0.25, 0.045, 72, 2.4),
}
_DEFAULT_PROFILE = (100.0, 0.05, 0.02, 80, 1.0)


class SyntheticSource(BarSource):
    info = FeedInfo(
        name="synthetic",
        provider="local",
        realtime=False,
        note="SYNTHETIC bars. Wiring tests only. Never evidence for a trade.",
    )

    def __init__(self, seed: int = 7) -> None:
        self.seed = seed

    def fetch(self, instrument: str, timeframe: str, limit: int = 300) -> List[Bar]:
        base, drift, vol, cycle_period, cycle_amp = _PROFILES.get(
            instrument.upper(), _DEFAULT_PROFILE
        )
        step = timeframe_seconds(timeframe)
        # Deterministic per instrument and timeframe so runs are reproducible.
        rng = random.Random("%s|%s|%d" % (instrument, timeframe, self.seed))

        bars_per_day = max(1.0, 86400.0 / step)
        per_bar_drift = drift / (252.0 * bars_per_day)
        per_bar_vol = vol / math.sqrt(bars_per_day)

        now = int(time.time() // step * step)
        start_ts = now - step * (limit - 1)

        price = base
        bars: List[Bar] = []
        for i in range(limit):
            shock = rng.gauss(0.0, 1.0) * per_bar_vol
            cycle = (cycle_amp / 100.0) * math.sin(2.0 * math.pi * i / cycle_period)
            price = max(0.01, price * (1.0 + per_bar_drift + shock + cycle / cycle_period))

            wiggle = abs(rng.gauss(0.0, per_bar_vol)) * price
            open_price = price * (1.0 + rng.gauss(0.0, per_bar_vol) * 0.3)
            high = max(open_price, price) + wiggle
            low = min(open_price, price) - wiggle
            volume = max(1.0, rng.gauss(1_000_000.0, 250_000.0))
            # Occasional volume surge so breakout confirmation can trigger.
            if rng.random() < 0.08:
                volume *= rng.uniform(1.6, 3.0)

            bars.append(
                Bar(
                    ts=float(start_ts + i * step),
                    open=round(open_price, 6),
                    high=round(max(high, low + 1e-6), 6),
                    low=round(max(low, 0.001), 6),
                    close=round(price, 6),
                    volume=round(volume, 2),
                )
            )
        return bars


__all__ = ["SyntheticSource"]
