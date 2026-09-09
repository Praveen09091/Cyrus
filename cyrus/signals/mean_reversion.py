"""Mean reversion for the index book (SPY, QQQ) on 15-minute bars.

Indices overextend and snap back. The trade fades the stretch and exits at the
mean. The whole edge lives in the exit, so the target is the moving average,
not a fantasy multiple.

This strategy is wrong in exactly one situation: when the stretch is the start
of a trend rather than an overshoot. The bear case says so in every proposal,
and the ATR stop is what pays for being wrong.
"""

from __future__ import annotations

from typing import Optional

from cyrus.bus.messages import Proposal, Side
from cyrus.indicators.core import atr, closes, last_valid, rsi, sma, zscore
from cyrus.signals.base import Strategy, StrategyContext


class MeanReversion(Strategy):
    name = "mean_reversion_z"
    style = "mean_reversion"
    min_bars = 60

    def evaluate(self, ctx: StrategyContext) -> Optional[Proposal]:
        lookback = ctx.iparam("lookback", 20)
        entry_z = ctx.fparam("entry_z", 1.5)
        atr_period = ctx.iparam("atr_period", 14)
        stop_mult = ctx.fparam("atr_stop_mult", 2.0)
        min_edge_atr = ctx.fparam("min_edge_atr", 0.5)

        price_series = closes(ctx.bars)
        z_series = zscore(price_series, lookback)
        mean_series = sma(price_series, lookback)
        atr_series = atr(ctx.bars, atr_period)
        rsi_series = rsi(price_series, 14)

        z = z_series[-1]
        mean = mean_series[-1]
        atr_now = last_valid(atr_series)
        rsi_now = last_valid(rsi_series)
        price = price_series[-1]

        if z is None or mean is None or atr_now is None or atr_now <= 0:
            return None
        if abs(z) < entry_z:
            return None

        # A z-score is scale-free, so a dead-quiet series still throws large
        # readings on pure noise. Require the trip back to the mean to be worth
        # more than one bar of normal movement, or there is nothing to collect.
        if abs(mean - price) < min_edge_atr * atr_now:
            return None

        indicators = {
            "z": round(z, 3),
            "mean": round(mean, 4),
            "atr": round(atr_now, 4),
            "rsi": round(rsi_now, 2) if rsi_now is not None else -1.0,
            "entry_z": entry_z,
        }

        stop_distance = stop_mult * atr_now

        if z <= -entry_z:
            # Stretched below the mean: fade the drop.
            stop = price - stop_distance
            if mean - price <= 0:
                return None
            return self._proposal(
                ctx,
                side=Side.LONG,
                entry=price,
                stop=stop,
                target=mean,
                thesis=(
                    "%s is %.2f standard deviations below its %d-period mean on %s. "
                    "Index overshoots of this size have historically reverted toward "
                    "the mean at %.2f." % (ctx.instrument, z, lookback, ctx.timeframe, mean)
                ),
                bear_case=(
                    "The stretch is the opening leg of a trend, not an overshoot. "
                    "In a risk-off regime this fade sells volatility into a decline "
                    "and stops out repeatedly. RSI at %s gives no trend context on its own."
                    % (("%.1f" % rsi_now) if rsi_now is not None else "n/a")
                ),
                invalidation="Close below %.4f (%.1f ATR under entry)." % (stop, stop_mult),
                indicators=indicators,
            )

        # Stretched above the mean: fade the pop.
        stop = price + stop_distance
        if price - mean <= 0:
            return None
        return self._proposal(
            ctx,
            side=Side.SHORT,
            entry=price,
            stop=stop,
            target=mean,
            thesis=(
                "%s is %.2f standard deviations above its %d-period mean on %s. "
                "The trade fades the extension back toward %.4f."
                % (ctx.instrument, z, lookback, ctx.timeframe, mean)
            ),
            bear_case=(
                "Shorting an index into strength has an unbounded loss tail and fights "
                "the long-run drift. A momentum breakout turns this into a trend fight."
            ),
            invalidation="Close above %.4f (%.1f ATR over entry)." % (stop, stop_mult),
            indicators=indicators,
        )


__all__ = ["MeanReversion"]
