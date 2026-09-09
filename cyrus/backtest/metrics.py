"""Backtest metrics.

Computed from the realised trade list and the equity curve it produced. No
metric here is annualised by assumption: the caller supplies the number of
periods per year for the timeframe actually tested, because guessing it is how
a 15-minute strategy ends up reporting a Sharpe built on daily arithmetic.

A metric that cannot be computed returns None rather than zero. Zero is a
value; None is the absence of one, and the difference matters when the number
is about to be used for sizing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence


@dataclass
class Metrics:
    trade_count: int = 0
    wins: int = 0
    losses: int = 0
    scratches: int = 0

    gross_win: float = 0.0
    gross_loss: float = 0.0
    net_pnl: float = 0.0
    fees_paid: float = 0.0

    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    payoff_ratio: Optional[float] = None
    profit_factor: Optional[float] = None
    expectancy: float = 0.0

    max_drawdown_pct: float = 0.0
    return_pct: float = 0.0
    sharpe: Optional[float] = None

    avg_bars_held: float = 0.0
    exit_reasons: Dict[str, int] = field(default_factory=dict)

    @property
    def has_edge(self) -> bool:
        """Positive expectancy after costs, over a sample worth believing.

        Thirty trades is not proof. It is the floor below which the number is
        pure noise and should not be shown to a sizing function at all.
        """
        return self.expectancy > 0.0 and self.trade_count >= 30

    def summary(self) -> str:
        return (
            "%d trades, %.1f%% win rate, expectancy $%.2f, profit factor %s, "
            "max drawdown %.2f%%, net $%.2f after $%.2f fees"
            % (
                self.trade_count,
                self.win_rate * 100.0,
                self.expectancy,
                "%.2f" % self.profit_factor if self.profit_factor is not None else "n/a",
                self.max_drawdown_pct,
                self.net_pnl,
                self.fees_paid,
            )
        )


def compute(
    pnls: Sequence[float],
    equity_curve: Sequence[float],
    fees: float = 0.0,
    bars_held: Optional[Sequence[int]] = None,
    exit_reasons: Optional[Sequence[str]] = None,
    periods_per_year: Optional[int] = None,
) -> Metrics:
    """Build the metric set from realised results.

    ``pnls`` are net of fees already; ``fees`` is reported separately so the
    cost drag is visible rather than buried in the net.
    """
    metrics = Metrics()
    metrics.trade_count = len(pnls)
    metrics.fees_paid = fees

    if not pnls:
        metrics.max_drawdown_pct = _max_drawdown_pct(equity_curve)
        metrics.return_pct = _return_pct(equity_curve)
        return metrics

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    metrics.wins = len(wins)
    metrics.losses = len(losses)
    metrics.scratches = len(pnls) - len(wins) - len(losses)

    metrics.gross_win = sum(wins)
    metrics.gross_loss = abs(sum(losses))
    metrics.net_pnl = sum(pnls)

    metrics.win_rate = len(wins) / len(pnls)
    metrics.avg_win = (metrics.gross_win / len(wins)) if wins else 0.0
    metrics.avg_loss = (metrics.gross_loss / len(losses)) if losses else 0.0
    metrics.expectancy = metrics.net_pnl / len(pnls)

    # A payoff ratio with no losses is undefined, not infinite. Reporting a
    # huge number here would flatter a sample that simply has not lost yet.
    metrics.payoff_ratio = (
        (metrics.avg_win / metrics.avg_loss) if metrics.avg_loss > 0 else None
    )
    metrics.profit_factor = (
        (metrics.gross_win / metrics.gross_loss) if metrics.gross_loss > 0 else None
    )

    metrics.max_drawdown_pct = _max_drawdown_pct(equity_curve)
    metrics.return_pct = _return_pct(equity_curve)
    metrics.sharpe = _sharpe(equity_curve, periods_per_year)

    if bars_held:
        metrics.avg_bars_held = sum(bars_held) / len(bars_held)
    if exit_reasons:
        counts: Dict[str, int] = {}
        for reason in exit_reasons:
            counts[reason] = counts.get(reason, 0) + 1
        metrics.exit_reasons = counts

    return metrics


def trades_per_year(
    first_ts: Optional[float], last_ts: Optional[float], trade_count: int
) -> Optional[int]:
    """Annualisation factor derived from the tested period, not assumed.

    The equity curve here advances one step per trade, so annualising it needs
    trades per year rather than bars per year. Both are computed from the real
    timestamps of the data that was tested; a strategy that fired twice over
    three years gets the small number it earned.
    """
    if first_ts is None or last_ts is None or trade_count < 2:
        return None
    span_seconds = last_ts - first_ts
    if span_seconds <= 0:
        return None
    years = span_seconds / (365.25 * 24 * 3600)
    if years <= 0:
        return None
    per_year = int(round(trade_count / years))
    return per_year if per_year >= 2 else None


def _max_drawdown_pct(equity_curve: Sequence[float]) -> float:
    peak = float("-inf")
    worst = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak * 100.0)
    return worst


def _return_pct(equity_curve: Sequence[float]) -> float:
    if len(equity_curve) < 2 or equity_curve[0] <= 0:
        return 0.0
    return (equity_curve[-1] / equity_curve[0] - 1.0) * 100.0


def _sharpe(equity_curve: Sequence[float], periods_per_year: Optional[int]) -> Optional[float]:
    """Sharpe from equity-curve returns, annualised only if told how.

    Without ``periods_per_year`` this returns None instead of silently
    assuming 252, which would misstate any intraday result.
    """
    if periods_per_year is None or len(equity_curve) < 3:
        return None
    returns: List[float] = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]
        if prev <= 0:
            return None
        returns.append(equity_curve[i] / prev - 1.0)
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    stdev = math.sqrt(variance)
    if stdev <= 1e-12:
        return None
    return (mean / stdev) * math.sqrt(periods_per_year)


__all__ = ["Metrics", "compute", "trades_per_year"]
