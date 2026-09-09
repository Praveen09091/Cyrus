"""Yahoo Finance adapter via yfinance.

Free and broad, and DELAYED. That is fine for research, planning, and the
4-hour and daily books. It is not fine for reacting to a live 15-minute close,
which is why ``info.realtime`` is False and the label travels with every bar
the desk uses.

Importing this module raises ImportError when yfinance is absent. The registry
treats that as "feed unavailable" and falls back rather than failing the cycle.
"""

from __future__ import annotations

from typing import List

from cyrus.data.sources import BarSource, FeedInfo
from cyrus.indicators.core import Bar

try:
    import yfinance  # type: ignore
except ImportError as exc:  # pragma: no cover - environment dependent
    raise ImportError(
        "yfinance is not installed. Run: python3 -m pip install yfinance"
    ) from exc

# yfinance intraday history is limited by interval; asking for more silently
# truncates, so the desk asks for what each interval can actually serve.
_PERIOD_FOR_INTERVAL = {
    "1m": "7d",
    "5m": "60d",
    "15m": "60d",
    "30m": "60d",
    "1h": "730d",
    "4h": "730d",
    "1d": "5y",
}

# Yahoo has no native 4h bar; request 1h and resample.
_NATIVE_INTERVAL = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "4h": "1h",
    "1d": "1d",
}


class YahooSource(BarSource):
    info = FeedInfo(
        name="yahoo",
        provider="yahoo",
        realtime=False,
        note="DELAYED free data. Acceptable for research and slow books only.",
    )

    def fetch(self, instrument: str, timeframe: str, limit: int = 300) -> List[Bar]:
        interval = _NATIVE_INTERVAL.get(timeframe)
        if interval is None:
            return []
        period = _PERIOD_FOR_INTERVAL.get(timeframe, "60d")

        ticker = yfinance.Ticker(instrument)
        frame = ticker.history(period=period, interval=interval, auto_adjust=False)
        if frame is None or frame.empty:
            return []

        bars: List[Bar] = []
        for index, row in frame.iterrows():
            try:
                close = float(row["Close"])
            except (KeyError, TypeError, ValueError):
                continue
            if close != close:  # NaN guard: a hole in the feed is not a price
                continue
            bars.append(
                Bar(
                    ts=index.timestamp(),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=close,
                    volume=float(row.get("Volume", 0.0) or 0.0),
                )
            )

        if timeframe == "4h":
            bars = resample(bars, 4)
        return bars[-limit:]


def resample(bars: List[Bar], factor: int) -> List[Bar]:
    """Aggregate N bars into one. Drops a trailing partial group.

    A partial bar is a bar that has not happened yet. Trading it is how a
    backtest quietly becomes a lie.
    """
    if factor <= 1 or not bars:
        return bars
    out: List[Bar] = []
    for start in range(0, len(bars) - factor + 1, factor):
        group = bars[start : start + factor]
        out.append(
            Bar(
                ts=group[0].ts,
                open=group[0].open,
                high=max(b.high for b in group),
                low=min(b.low for b in group),
                close=group[-1].close,
                volume=sum(b.volume for b in group),
            )
        )
    return out


__all__ = ["YahooSource", "resample"]
