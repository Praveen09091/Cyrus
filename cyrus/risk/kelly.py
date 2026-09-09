"""Kelly sizing, deliberately crippled.

Full Kelly maximises long-run growth only if the edge estimate is exact. It
never is. Kelly is also brutally asymmetric: overbetting by 2x turns a
positive-edge system into a losing one, while underbetting only slows you
down. So the desk uses a fraction of Kelly and treats the estimate itself as
suspect when the sample is thin or in-sample.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class KellyEstimate:
    raw: float                # unconstrained Kelly fraction of equity
    applied: float            # after the desk fraction and sample discount
    win_rate: float
    payoff_ratio: float
    sample_size: int
    out_of_sample: bool
    discount: float
    note: str

    @property
    def has_edge(self) -> bool:
        return self.raw > 0.0


def kelly_fraction(win_rate: float, win_payoff: float, loss_payoff: float = 1.0) -> float:
    """Kelly for a two-outcome bet.

    f* = (p * b - q) / b, where b is the win/loss payoff ratio.
    Returns 0.0 when the edge is absent; a negative Kelly means do not bet,
    not bet the other way, because the reverse trade has its own costs.
    """
    if not 0.0 < win_rate < 1.0:
        return 0.0
    if win_payoff <= 0 or loss_payoff <= 0:
        return 0.0
    b = win_payoff / loss_payoff
    loss_rate = 1.0 - win_rate
    f = (win_rate * b - loss_rate) / b
    return max(f, 0.0)


def sample_discount(sample_size: int, out_of_sample: bool) -> float:
    """Shrink the estimate toward zero when the evidence is thin.

    A 60% win rate over 12 trades is noise. This is the difference between a
    backtest that looks like an edge and one that is an edge.
    """
    if sample_size <= 0:
        return 0.0
    if sample_size < 30:
        base = 0.25
    elif sample_size < 100:
        base = 0.50
    elif sample_size < 300:
        base = 0.75
    else:
        base = 1.0
    if not out_of_sample:
        # In-sample results are a description of the past, not a forecast.
        base *= 0.5
    return base


def estimate(
    win_rate: float,
    win_payoff: float,
    loss_payoff: float = 1.0,
    sample_size: int = 0,
    out_of_sample: bool = False,
    desk_fraction: float = 0.25,
    cap: Optional[float] = None,
) -> KellyEstimate:
    """Full pipeline: raw Kelly, sample discount, desk fraction, hard cap."""
    raw = kelly_fraction(win_rate, win_payoff, loss_payoff)
    discount = sample_discount(sample_size, out_of_sample)
    applied = raw * discount * max(min(desk_fraction, 0.5), 0.0)
    if cap is not None:
        applied = min(applied, cap)

    if raw <= 0.0:
        note = "No edge in the estimate. Kelly is zero, so the size is zero."
    elif discount == 0.0:
        note = "No trade history for this strategy. Size stays at zero until there is."
    elif not out_of_sample:
        note = (
            "In-sample estimate only, halved on principle. Sample of %d trades." % sample_size
        )
    else:
        note = "Out-of-sample estimate over %d trades, discounted %.0f%%." % (
            sample_size,
            (1.0 - discount) * 100.0,
        )

    return KellyEstimate(
        raw=raw,
        applied=applied,
        win_rate=win_rate,
        payoff_ratio=(win_payoff / loss_payoff) if loss_payoff else 0.0,
        sample_size=sample_size,
        out_of_sample=out_of_sample,
        discount=discount,
        note=note,
    )


__all__ = ["KellyEstimate", "kelly_fraction", "sample_discount", "estimate"]
