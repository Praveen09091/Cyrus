"""Deterministic indicator math.

Pure Python on purpose: no model call, no hidden state, no dependency drift.
Every function returns a series aligned to the input length, with ``None`` in
the warmup region so a caller can never silently trade a half-formed value.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence

Series = Sequence[float]
MaybeSeries = List[Optional[float]]


@dataclass
class Bar:
    """One OHLCV bar. ``ts`` is epoch seconds."""

    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def closes(bars: Sequence[Bar]) -> List[float]:
    return [b.close for b in bars]


def sma(values: Series, period: int) -> MaybeSeries:
    if period <= 0:
        raise ValueError("period must be positive")
    out: MaybeSeries = []
    running = 0.0
    for i, value in enumerate(values):
        running += value
        if i >= period:
            running -= values[i - period]
        out.append(running / period if i >= period - 1 else None)
    return out


def ema(values: Series, period: int) -> MaybeSeries:
    """Seeded with an SMA so the first emitted value is not a single close."""
    if period <= 0:
        raise ValueError("period must be positive")
    out: MaybeSeries = [None] * len(values)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = (values[i] - prev) * alpha + prev
        out[i] = prev
    return out


def rolling_std(values: Series, period: int) -> MaybeSeries:
    """Population standard deviation over a trailing window."""
    out: MaybeSeries = []
    for i in range(len(values)):
        if i < period - 1:
            out.append(None)
            continue
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        variance = sum((v - mean) ** 2 for v in window) / period
        out.append(math.sqrt(variance))
    return out


def zscore(values: Series, period: int) -> MaybeSeries:
    """Distance from the rolling mean in standard deviations.

    This is the mean-reversion trigger for the index book. A flat window
    yields ``None`` rather than a division blow-up.
    """
    means = sma(values, period)
    stds = rolling_std(values, period)
    out: MaybeSeries = []
    for i in range(len(values)):
        mean, std = means[i], stds[i]
        if mean is None or std is None or std <= 1e-12:
            out.append(None)
        else:
            out.append((values[i] - mean) / std)
    return out


def bollinger(values: Series, period: int = 20, mult: float = 2.0):
    """Returns (upper, mid, lower)."""
    mid = sma(values, period)
    std = rolling_std(values, period)
    upper: MaybeSeries = []
    lower: MaybeSeries = []
    for i in range(len(values)):
        if mid[i] is None or std[i] is None:
            upper.append(None)
            lower.append(None)
        else:
            upper.append(mid[i] + mult * std[i])
            lower.append(mid[i] - mult * std[i])
    return upper, mid, lower


def true_range(bars: Sequence[Bar]) -> MaybeSeries:
    out: MaybeSeries = [None]
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        high, low = bars[i].high, bars[i].low
        out.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return out


def atr(bars: Sequence[Bar], period: int = 14) -> MaybeSeries:
    """Wilder's ATR. Drives every stop distance and every position size."""
    tr = true_range(bars)
    out: MaybeSeries = [None] * len(bars)
    values = [v for v in tr if v is not None]
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    idx = period  # tr[0] is None, so the seed covers bars 1..period
    out[idx] = seed
    prev = seed
    for i in range(idx + 1, len(bars)):
        current = tr[i]
        if current is None:
            out[i] = prev
            continue
        prev = (prev * (period - 1) + current) / period
        out[i] = prev
    return out


def rsi(values: Series, period: int = 14) -> MaybeSeries:
    out: MaybeSeries = [None] * len(values)
    if len(values) <= period:
        return out
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = _rsi_from(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    if avg_loss <= 1e-12:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(values: Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """Returns (macd_line, signal_line, histogram)."""
    fast_ema, slow_ema = ema(values, fast), ema(values, slow)
    line: MaybeSeries = [
        None if (fast_ema[i] is None or slow_ema[i] is None) else fast_ema[i] - slow_ema[i]
        for i in range(len(values))
    ]
    dense = [v for v in line if v is not None]
    signal_dense = ema(dense, signal) if dense else []
    signal_line: MaybeSeries = [None] * len(values)
    offset = len(values) - len(dense)
    for i, value in enumerate(signal_dense):
        signal_line[offset + i] = value
    hist: MaybeSeries = [
        None if (line[i] is None or signal_line[i] is None) else line[i] - signal_line[i]
        for i in range(len(values))
    ]
    return line, signal_line, hist


def donchian(bars: Sequence[Bar], period: int = 20):
    """Breakout channel: (highest_high, lowest_low), excluding the current bar.

    Excluding the current bar matters. Including it means price is always at
    its own extreme and every bar looks like a breakout.
    """
    highs: MaybeSeries = [None] * len(bars)
    lows: MaybeSeries = [None] * len(bars)
    for i in range(len(bars)):
        if i < period:
            continue
        window = bars[i - period : i]
        highs[i] = max(b.high for b in window)
        lows[i] = min(b.low for b in window)
    return highs, lows


def volume_ratio(bars: Sequence[Bar], period: int = 20) -> MaybeSeries:
    """Current volume against its trailing average. Confirms a breakout."""
    vols = [b.volume for b in bars]
    avg = sma(vols, period)
    out: MaybeSeries = []
    for i in range(len(bars)):
        if avg[i] is None or avg[i] <= 1e-12:
            out.append(None)
        else:
            out.append(vols[i] / avg[i])
    return out


def ema_cross(values: Series, fast: int, slow: int) -> MaybeSeries:
    """+1 when fast is above slow, -1 when below, None during warmup."""
    fast_line, slow_line = ema(values, fast), ema(values, slow)
    out: MaybeSeries = []
    for i in range(len(values)):
        if fast_line[i] is None or slow_line[i] is None:
            out.append(None)
        else:
            out.append(1.0 if fast_line[i] > slow_line[i] else -1.0)
    return out


def realized_volatility(values: Series, period: int = 20, periods_per_year: int = 252) -> Optional[float]:
    """Annualised stdev of log returns over the last ``period`` observations."""
    if len(values) < period + 1:
        return None
    rets: List[float] = []
    for i in range(len(values) - period, len(values)):
        prev = values[i - 1]
        if prev <= 0 or values[i] <= 0:
            return None
        rets.append(math.log(values[i] / prev))
    mean = sum(rets) / len(rets)
    variance = sum((r - mean) ** 2 for r in rets) / len(rets)
    return math.sqrt(variance) * math.sqrt(periods_per_year)


def max_drawdown(equity: Series) -> float:
    """Worst peak-to-trough decline as a positive fraction."""
    peak = float("-inf")
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def last_valid(series: MaybeSeries) -> Optional[float]:
    for value in reversed(series):
        if value is not None:
            return value
    return None


__all__ = [
    "Bar",
    "closes",
    "sma",
    "ema",
    "rolling_std",
    "zscore",
    "bollinger",
    "true_range",
    "atr",
    "rsi",
    "macd",
    "donchian",
    "volume_ratio",
    "ema_cross",
    "realized_volatility",
    "max_drawdown",
    "last_valid",
]
