"""Backtest harness.

Exists so Oracle can size from a track record instead of refusing forever, and
so that record is one the desk has a right to believe: no look-ahead, real
costs, and parameters chosen only on data the score never came from.
"""

from cyrus.backtest.engine import BacktestEngine, BacktestResult, Trade
from cyrus.backtest.metrics import Metrics, compute, trades_per_year
from cyrus.backtest.walkforward import Fold, WalkForward, WalkForwardReport

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "Trade",
    "Metrics",
    "compute",
    "trades_per_year",
    "WalkForward",
    "WalkForwardReport",
    "Fold",
]
