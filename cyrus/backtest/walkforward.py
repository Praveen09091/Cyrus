"""Walk-forward validation.

A single backtest over a whole history tells you how a strategy would have done
if you had known the right parameters in advance. You did not. Walk-forward
removes that advantage: parameters are chosen on a training window and scored
only on the window that follows, which the selection never saw.

The honesty rule this module exists to enforce:

  - **With a parameter grid**, selection happens on train folds only, so the
    concatenated test results are genuinely out-of-sample and are labelled
    ``out_of_sample=True``.
  - **Without a grid**, nothing is fitted here, but a human still chose those
    parameters after looking at this market. That is not out-of-sample, and the
    report says so with ``out_of_sample=False`` and a stated reason.

Oracle reads that flag. Mislabelling it would let an in-sample fit through to
position sizing, which is the single most expensive lie this repo could tell.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from cyrus.backtest.engine import BacktestEngine, BacktestResult, Trade
from cyrus.backtest.metrics import Metrics, compute, trades_per_year
from cyrus.config import BookConfig, DeskConfig
from cyrus.indicators.core import Bar
from cyrus.signals.base import Strategy


@dataclass
class Fold:
    index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    chosen_params: Dict[str, Any] = field(default_factory=dict)
    selection_score: Optional[float] = None
    train_trades: int = 0
    test_trades: int = 0
    test_pnl: float = 0.0

    def describe(self) -> str:
        return (
            "fold %d: train[%d:%d] -> test[%d:%d], %d test trades, $%.2f"
            % (
                self.index,
                self.train_start,
                self.train_end,
                self.test_start,
                self.test_end,
                self.test_trades,
                self.test_pnl,
            )
        )


@dataclass
class WalkForwardReport:
    strategy: str
    instrument: str
    timeframe: str
    out_of_sample: bool
    evidence_note: str
    folds: List[Fold] = field(default_factory=list)
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    metrics: Metrics = field(default_factory=Metrics)
    starting_equity: float = 0.0
    bars_total: int = 0
    data_label: str = "unknown"
    grid_size: int = 0

    @property
    def usable_for_sizing(self) -> bool:
        """Only an out-of-sample result with a real edge may inform size.

        Everything else is description. Oracle checks this before it will grant
        a Kelly fraction above zero.
        """
        return self.out_of_sample and self.metrics.has_edge

    def summary(self) -> str:
        label = "OUT-OF-SAMPLE" if self.out_of_sample else "IN-SAMPLE"
        return "%s [%s] %s on %s %s: %s" % (
            label,
            self.data_label,
            self.strategy,
            self.instrument,
            self.timeframe,
            self.metrics.summary(),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "instrument": self.instrument,
            "timeframe": self.timeframe,
            "out_of_sample": self.out_of_sample,
            "evidence_note": self.evidence_note,
            "data_label": self.data_label,
            "grid_size": self.grid_size,
            "bars_total": self.bars_total,
            "starting_equity": self.starting_equity,
            "folds": [
                {
                    "index": f.index,
                    "train": [f.train_start, f.train_end],
                    "test": [f.test_start, f.test_end],
                    "chosen_params": f.chosen_params,
                    "selection_score": f.selection_score,
                    "train_trades": f.train_trades,
                    "test_trades": f.test_trades,
                    "test_pnl": f.test_pnl,
                }
                for f in self.folds
            ],
            "metrics": {
                "trade_count": self.metrics.trade_count,
                "wins": self.metrics.wins,
                "losses": self.metrics.losses,
                "win_rate": self.metrics.win_rate,
                "avg_win": self.metrics.avg_win,
                "avg_loss": self.metrics.avg_loss,
                "payoff_ratio": self.metrics.payoff_ratio,
                "profit_factor": self.metrics.profit_factor,
                "expectancy": self.metrics.expectancy,
                "net_pnl": self.metrics.net_pnl,
                "fees_paid": self.metrics.fees_paid,
                "max_drawdown_pct": self.metrics.max_drawdown_pct,
                "return_pct": self.metrics.return_pct,
                "sharpe": self.metrics.sharpe,
                "avg_bars_held": self.metrics.avg_bars_held,
                "exit_reasons": self.metrics.exit_reasons,
            },
            "trades": [t.to_dict() for t in self.trades],
        }


class WalkForward:
    """Anchored walk-forward: the training window grows, tests never overlap."""

    def __init__(
        self,
        config: DeskConfig,
        folds: int = 4,
        starting_equity: Optional[float] = None,
        periods_per_year: Optional[int] = None,
        selection_metric: str = "expectancy",
        min_train_trades: int = 5,
    ) -> None:
        if folds < 2:
            raise ValueError("walk-forward needs at least 2 folds")
        self.config = config
        self.folds = folds
        self.starting_equity = starting_equity or config.equity
        self.periods_per_year = periods_per_year
        self.selection_metric = selection_metric
        self.min_train_trades = min_train_trades

    def run(
        self,
        strategy_factory,
        book: BookConfig,
        instrument: str,
        bars: Sequence[Bar],
        grid: Optional[Dict[str, List[Any]]] = None,
        data_label: str = "unknown",
    ) -> WalkForwardReport:
        """``strategy_factory`` is a zero-arg callable returning a fresh Strategy."""
        probe: Strategy = strategy_factory()
        combos = _expand_grid(grid)
        optimising = len(combos) > 1

        report = WalkForwardReport(
            strategy=probe.name,
            instrument=instrument,
            timeframe=book.timeframe,
            out_of_sample=optimising,
            evidence_note=(
                "Parameters selected on training folds only; test folds were unseen "
                "at selection time."
                if optimising
                else "No parameter grid supplied, so nothing was fitted here. The "
                "parameters were still chosen by a human who has seen this market, "
                "so these results are in-sample and must not inform position size."
            ),
            starting_equity=self.starting_equity,
            bars_total=len(bars),
            data_label=data_label,
            grid_size=len(combos),
        )

        window = _plan_windows(len(bars), self.folds, probe.min_bars)
        if not window:
            report.evidence_note = (
                "Not enough bars for %d folds with a %d-bar warmup. No result produced."
                % (self.folds, probe.min_bars)
            )
            report.out_of_sample = False
            return report

        equity = self.starting_equity
        report.equity_curve.append(equity)
        all_pnls: List[float] = []
        all_bars_held: List[int] = []
        all_exit_reasons: List[str] = []
        total_fees = 0.0

        for fold_index, (train_start, train_end, test_start, test_end) in enumerate(window):
            fold = Fold(
                index=fold_index,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
            )

            chosen_params, score, train_trades = self._select(
                strategy_factory, book, instrument, bars[train_start:train_end], combos, equity
            )
            fold.chosen_params = dict(chosen_params)
            fold.selection_score = score
            fold.train_trades = train_trades

            # The test window carries the warmup it needs, but the strategy is
            # only allowed to act from test_start onward.
            engine = BacktestEngine(
                config=self.config,
                starting_equity=equity,
                periods_per_year=self.periods_per_year,
            )
            test_result = engine.run(
                strategy=strategy_factory(),
                book=book,
                instrument=instrument,
                bars=bars[max(0, test_start - probe.min_bars) : test_end],
                params=chosen_params,
                data_label=data_label,
            )

            for trade in test_result.trades:
                report.trades.append(trade)
                all_pnls.append(trade.net_pnl)
                all_bars_held.append(trade.bars_held)
                all_exit_reasons.append(trade.exit_reason)
                total_fees += trade.fees
                equity += trade.net_pnl
                report.equity_curve.append(equity)

            fold.test_trades = len(test_result.trades)
            fold.test_pnl = sum(t.net_pnl for t in test_result.trades)
            report.folds.append(fold)

        report.metrics = compute(
            pnls=all_pnls,
            equity_curve=report.equity_curve,
            fees=total_fees,
            bars_held=all_bars_held,
            exit_reasons=all_exit_reasons,
            periods_per_year=self.periods_per_year
            or trades_per_year(
                report.trades[0].entry_ts if report.trades else None,
                report.trades[-1].exit_ts if report.trades else None,
                len(all_pnls),
            ),
        )
        return report

    # --- parameter selection ---------------------------------------------

    def _select(
        self,
        strategy_factory,
        book: BookConfig,
        instrument: str,
        train_bars: Sequence[Bar],
        combos: List[Dict[str, Any]],
        equity: float,
    ) -> Tuple[Dict[str, Any], Optional[float], int]:
        """Pick parameters using the training window only."""
        base = dict(book.params)
        if len(combos) <= 1:
            merged = dict(base)
            merged.update(combos[0] if combos else {})
            engine = BacktestEngine(self.config, starting_equity=equity)
            probe = engine.run(strategy_factory(), book, instrument, train_bars, params=merged)
            return merged, None, probe.metrics.trade_count

        best_params: Optional[Dict[str, Any]] = None
        best_score: Optional[float] = None
        best_trades = 0

        for combo in combos:
            merged = dict(base)
            merged.update(combo)
            engine = BacktestEngine(self.config, starting_equity=equity)
            outcome = engine.run(
                strategy_factory(), book, instrument, train_bars, params=merged
            )
            # A parameter set that barely traded has not earned selection, no
            # matter how good the handful of trades looked.
            if outcome.metrics.trade_count < self.min_train_trades:
                continue
            score = self._score(outcome)
            if score is None:
                continue
            if best_score is None or score > best_score:
                best_score = score
                best_params = merged
                best_trades = outcome.metrics.trade_count

        if best_params is None:
            # Nothing cleared the bar. Fall back to the configured defaults and
            # say so through a null score rather than inventing a winner.
            return dict(base), None, 0
        return best_params, best_score, best_trades

    def _score(self, result: BacktestResult) -> Optional[float]:
        metrics = result.metrics
        if self.selection_metric == "expectancy":
            return metrics.expectancy
        if self.selection_metric == "profit_factor":
            return metrics.profit_factor
        if self.selection_metric == "net_pnl":
            return metrics.net_pnl
        if self.selection_metric == "sharpe":
            return metrics.sharpe
        raise ValueError("unknown selection metric %r" % self.selection_metric)


def _expand_grid(grid: Optional[Dict[str, List[Any]]]) -> List[Dict[str, Any]]:
    if not grid:
        return [{}]
    keys = sorted(grid)
    combos: List[Dict[str, Any]] = []
    for values in itertools.product(*(grid[k] for k in keys)):
        combos.append(dict(zip(keys, values)))
    return combos


def _plan_windows(total: int, folds: int, warmup: int) -> List[Tuple[int, int, int, int]]:
    """Anchored windows: train grows from the start, each test is the next slice.

    Returns [] when there is not enough history, rather than shrinking the
    warmup to force a result out of data that cannot support one.
    """
    usable = total - warmup
    if usable <= 0:
        return []
    test_size = usable // (folds + 1)
    if test_size < 2:
        return []

    windows: List[Tuple[int, int, int, int]] = []
    for k in range(1, folds + 1):
        train_end = warmup + test_size * k
        test_start = train_end
        test_end = min(total, test_start + test_size)
        if test_end - test_start < 2:
            break
        if train_end <= warmup:
            continue
        windows.append((0, train_end, test_start, test_end))
    return windows


__all__ = ["WalkForward", "WalkForwardReport", "Fold"]
