"""Trend following for the commodity book (GLD, USO) on 4-hour bars.

Commodities move in longer, cleaner waves than indices, so the signal is a
slow EMA cross and the stop is wide. The point of a wide stop here is to stay
in a move that breathes, not to be brave.

Only fresh crosses fire. An already-extended trend is not an entry: it is a
position someone else already owns.
"""

from __future__ import annotations

from typing import Optional

from cyrus.bus.messages import Proposal, Side
from cyrus.indicators.core import atr, closes, ema, last_valid
from cyrus.signals.base import Strategy, StrategyContext


class TrendFollow(Strategy):
    name = "ema_trend"
    style = "trend"
    min_bars = 220

    def evaluate(self, ctx: StrategyContext) -> Optional[Proposal]:
        fast_period = ctx.iparam("fast_ema", 50)
        slow_period = ctx.iparam("slow_ema", 200)
        atr_period = ctx.iparam("atr_period", 14)
        stop_mult = ctx.fparam("atr_stop_mult", 3.0)
        max_bars_since_cross = ctx.iparam("max_bars_since_cross", 5)

        price_series = closes(ctx.bars)
        fast = ema(price_series, fast_period)
        slow = ema(price_series, slow_period)
        atr_series = atr(ctx.bars, atr_period)
        atr_now = last_valid(atr_series)
        price = price_series[-1]

        if atr_now is None or atr_now <= 0:
            return None

        state = _cross_state(fast, slow)
        if state is None:
            return None
        direction, bars_since_cross = state

        # Chase nothing. A cross five bars old is context, not a trigger.
        if bars_since_cross > max_bars_since_cross:
            return None

        indicators = {
            "fast_ema": round(fast[-1], 4) if fast[-1] is not None else 0.0,
            "slow_ema": round(slow[-1], 4) if slow[-1] is not None else 0.0,
            "atr": round(atr_now, 4),
            "bars_since_cross": float(bars_since_cross),
        }

        stop_distance = stop_mult * atr_now

        if direction > 0:
            return self._proposal(
                ctx,
                side=Side.LONG,
                entry=price,
                stop=price - stop_distance,
                target=price + 3.0 * stop_distance,
                thesis=(
                    "%s crossed its %d EMA above its %d EMA %d bars ago on %s. "
                    "Commodity trends persist long enough that a wide-stop long can ride "
                    "the wave rather than scalp it."
                    % (ctx.instrument, fast_period, slow_period, bars_since_cross, ctx.timeframe)
                ),
                bear_case=(
                    "EMA crosses are lagging by construction and whipsaw in a range. "
                    "Energy and metals also carry headline risk that gaps straight "
                    "through a %.1f ATR stop." % stop_mult
                ),
                invalidation=(
                    "Close below %.4f, or the %d EMA crossing back under the %d EMA."
                    % (price - stop_distance, fast_period, slow_period)
                ),
                indicators=indicators,
            )

        return self._proposal(
            ctx,
            side=Side.SHORT,
            entry=price,
            stop=price + stop_distance,
            target=price - 3.0 * stop_distance,
            thesis=(
                "%s crossed its %d EMA below its %d EMA %d bars ago on %s, which has "
                "marked the start of sustained downtrends in this book."
                % (ctx.instrument, fast_period, slow_period, bars_since_cross, ctx.timeframe)
            ),
            bear_case=(
                "Commodity shorts face supply shocks and geopolitical spikes that move "
                "faster than any stop. Contango and roll cost work against a held short."
            ),
            invalidation=(
                "Close above %.4f, or the %d EMA crossing back over the %d EMA."
                % (price + stop_distance, fast_period, slow_period)
            ),
            indicators=indicators,
        )


def _cross_state(fast, slow):
    """Returns (direction, bars_since_cross) or None during warmup."""
    pairs = [
        (i, fast[i], slow[i])
        for i in range(len(fast))
        if fast[i] is not None and slow[i] is not None
    ]
    if len(pairs) < 2:
        return None

    latest_sign = 1 if pairs[-1][1] > pairs[-1][2] else -1
    bars_since = 0
    for i in range(len(pairs) - 2, -1, -1):
        sign = 1 if pairs[i][1] > pairs[i][2] else -1
        if sign != latest_sign:
            break
        bars_since += 1
    else:
        # No cross anywhere in the series: the state is not fresh.
        return None
    return latest_sign, bars_since


__all__ = ["TrendFollow"]
