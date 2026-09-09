"""Momentum breakout for the crypto book (BTC, ETH) on hourly bars.

Crypto trends harder than indices, so the trade rides the break instead of
fading it. Two filters do the real work: the channel must break on the close,
and volume must confirm. Without volume, a breakout is usually a wick.

The stop is wider than the index book because there are no circuit breakers
and the market never closes. The config pays for that with a smaller risk cap.
"""

from __future__ import annotations

from typing import Optional

from cyrus.bus.messages import Proposal, Side
from cyrus.indicators.core import atr, closes, donchian, last_valid, volume_ratio
from cyrus.signals.base import Strategy, StrategyContext


class MomentumBreakout(Strategy):
    name = "momentum_breakout"
    style = "momentum"
    min_bars = 60

    def evaluate(self, ctx: StrategyContext) -> Optional[Proposal]:
        lookback = ctx.iparam("breakout_lookback", 20)
        vol_mult = ctx.fparam("volume_mult", 1.5)
        atr_period = ctx.iparam("atr_period", 14)
        stop_mult = ctx.fparam("atr_stop_mult", 2.5)
        trail_mult = ctx.fparam("trail_atr_mult", 2.0)

        price_series = closes(ctx.bars)
        highs, lows = donchian(ctx.bars, lookback)
        atr_series = atr(ctx.bars, atr_period)
        vol_series = volume_ratio(ctx.bars, lookback)

        price = price_series[-1]
        channel_high, channel_low = highs[-1], lows[-1]
        atr_now = last_valid(atr_series)
        vol_now = vol_series[-1]

        if channel_high is None or channel_low is None or atr_now is None or atr_now <= 0:
            return None

        # No volume series means no confirmation, which means no trade.
        if vol_now is None or vol_now < vol_mult:
            return None

        indicators = {
            "channel_high": round(channel_high, 4),
            "channel_low": round(channel_low, 4),
            "atr": round(atr_now, 4),
            "volume_ratio": round(vol_now, 2),
            "volume_mult_required": vol_mult,
        }

        if price > channel_high:
            stop = price - stop_mult * atr_now
            target = price + 2.0 * stop_mult * atr_now
            return self._proposal(
                ctx,
                side=Side.LONG,
                entry=price,
                stop=stop,
                target=target,
                thesis=(
                    "%s closed at %.4f, above its %d-period high of %.4f, on %.1fx "
                    "average volume. Crypto breakouts with volume confirmation tend to "
                    "extend rather than immediately revert."
                    % (ctx.instrument, price, lookback, channel_high, vol_now)
                ),
                bear_case=(
                    "Breakouts fail more often than they run, and a failed break traps "
                    "the entry at the extreme. The market trades 24/7 with no halts, so "
                    "a weekend gap can jump the %.1f ATR stop entirely." % stop_mult
                ),
                invalidation=(
                    "Close below %.4f, or a trailing stop %.1f ATR under the running high."
                    % (stop, trail_mult)
                ),
                indicators=indicators,
            )

        if price < channel_low:
            stop = price + stop_mult * atr_now
            target = price - 2.0 * stop_mult * atr_now
            return self._proposal(
                ctx,
                side=Side.SHORT,
                entry=price,
                stop=stop,
                target=target,
                thesis=(
                    "%s closed at %.4f, below its %d-period low of %.4f, on %.1fx "
                    "average volume. Downside breaks with volume tend to extend."
                    % (ctx.instrument, price, lookback, channel_low, vol_now)
                ),
                bear_case=(
                    "Short crypto carries funding cost and violent short-squeeze risk. "
                    "Liquidation cascades reverse faster than a stop can fill."
                ),
                invalidation=(
                    "Close above %.4f, or a trailing stop %.1f ATR above the running low."
                    % (stop, trail_mult)
                ),
                indicators=indicators,
            )

        return None


__all__ = ["MomentumBreakout"]
