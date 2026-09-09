"""Backtest engine.

The design constraint that matters is look-ahead. A decision made on bar ``i``
may only see bars ``0..i``, and it fills at the open of bar ``i+1``. Anything
else quietly tests a strategy that could see the future, which is the standard
way a backtest produces a number nobody can trade.

Three further rules keep the result honest:

  - **Gaps cost money.** If a bar opens through the stop, the fill is the open,
    not the stop level. Stops do not fill where you wrote them.
  - **Ambiguity resolves against the trade.** When one bar touches both the
    stop and the target, the engine assumes the stop hit first.
  - **Costs are config, not decoration.** Slippage and commission come from
    ``ExecutionConfig`` and always work against the position.

Sizing runs through the same ``size_proposal`` the live desk uses, against a
compounding equity curve, so a backtest cannot pass a size the risk kernel
would have refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from cyrus.backtest.metrics import Metrics, compute, trades_per_year
from cyrus.bus.messages import Proposal, Side
from cyrus.config import BookConfig, Costs, DeskConfig
from cyrus.indicators.core import Bar
from cyrus.risk.kelly import KellyEstimate
from cyrus.risk.sizing import size_proposal
from cyrus.risk.state import PortfolioState
from cyrus.signals.base import Strategy, StrategyContext


@dataclass
class Trade:
    """One completed round trip, with the costs it actually paid."""

    instrument: str
    strategy: str
    side: Side
    entry_ts: float
    exit_ts: float
    entry: float
    exit: float
    stop: float
    target: float
    quantity: float
    gross_pnl: float
    fees: float
    net_pnl: float
    bars_held: int
    exit_reason: str  # stop | target | timeout | end_of_data
    entry_bar: int
    mae: float = 0.0  # worst adverse excursion, in price
    mfe: float = 0.0  # best favourable excursion, in price

    def to_dict(self) -> Dict[str, object]:
        return {
            "instrument": self.instrument,
            "strategy": self.strategy,
            "side": self.side.value,
            "entry_ts": self.entry_ts,
            "exit_ts": self.exit_ts,
            "entry": self.entry,
            "exit": self.exit,
            "stop": self.stop,
            "target": self.target,
            "quantity": self.quantity,
            "gross_pnl": self.gross_pnl,
            "fees": self.fees,
            "net_pnl": self.net_pnl,
            "bars_held": self.bars_held,
            "exit_reason": self.exit_reason,
            "mae": self.mae,
            "mfe": self.mfe,
        }


@dataclass
class BacktestResult:
    strategy: str
    instrument: str
    timeframe: str
    starting_equity: float
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    metrics: Metrics = field(default_factory=Metrics)
    bars_tested: int = 0
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    skipped_no_size: int = 0
    skipped_gap_through_stop: int = 0
    data_label: str = "unknown"

    @property
    def ending_equity(self) -> float:
        return self.equity_curve[-1] if self.equity_curve else self.starting_equity

    def summary(self) -> str:
        return "%s on %s %s: %s" % (
            self.strategy,
            self.instrument,
            self.timeframe,
            self.metrics.summary(),
        )


class BacktestEngine:
    """Replays bars through one strategy on one instrument."""

    def __init__(
        self,
        config: DeskConfig,
        starting_equity: Optional[float] = None,
        periods_per_year: Optional[int] = None,
        max_holding_bars: Optional[int] = None,
    ) -> None:
        self.config = config
        self.starting_equity = starting_equity or config.equity
        self.periods_per_year = periods_per_year
        self.max_holding_bars = max_holding_bars

    def run(
        self,
        strategy: Strategy,
        book: BookConfig,
        instrument: str,
        bars: Sequence[Bar],
        params: Optional[Dict[str, object]] = None,
        kelly: Optional[KellyEstimate] = None,
        data_label: str = "unknown",
    ) -> BacktestResult:
        params = params if params is not None else book.params
        # Resolved once from the book actually passed in, so the costs charged
        # are the ones the caller declared rather than a name lookup.
        costs = self.config.costs_for(book)
        result = BacktestResult(
            strategy=strategy.name,
            instrument=instrument,
            timeframe=book.timeframe,
            starting_equity=self.starting_equity,
            bars_tested=len(bars),
            first_ts=bars[0].ts if bars else None,
            last_ts=bars[-1].ts if bars else None,
            data_label=data_label,
        )

        equity = self.starting_equity
        result.equity_curve.append(equity)
        state = PortfolioState(equity=equity)

        pnls: List[float] = []
        bars_held: List[int] = []
        exit_reasons: List[str] = []
        total_fees = 0.0

        i = strategy.min_bars
        last_index = len(bars) - 1

        while i < last_index:
            # Decision uses only bars that have closed. Slicing here is what
            # makes look-ahead structurally impossible rather than a promise.
            visible = bars[: i + 1]
            proposal = strategy(
                StrategyContext(
                    book=book.name,
                    instrument=instrument,
                    timeframe=book.timeframe,
                    bars=visible,
                    params=params,
                )
            )
            if proposal is None:
                i += 1
                continue

            trade = self._simulate(
                proposal=proposal,
                bars=bars,
                signal_index=i,
                state=state,
                kelly=kelly,
                result=result,
                costs=costs,
            )
            if trade is None:
                i += 1
                continue

            equity += trade.net_pnl
            state.mark_equity(equity)
            result.equity_curve.append(equity)
            result.trades.append(trade)
            pnls.append(trade.net_pnl)
            bars_held.append(trade.bars_held)
            exit_reasons.append(trade.exit_reason)
            total_fees += trade.fees

            # Resume after the exit: one position per instrument at a time.
            i = trade.entry_bar + trade.bars_held + 1

        result.metrics = compute(
            pnls=pnls,
            equity_curve=result.equity_curve,
            fees=total_fees,
            bars_held=bars_held,
            exit_reasons=exit_reasons,
            periods_per_year=self.periods_per_year
            or trades_per_year(result.first_ts, result.last_ts, len(pnls)),
        )
        return result

    # --- one trade --------------------------------------------------------

    def _simulate(
        self,
        proposal: Proposal,
        bars: Sequence[Bar],
        signal_index: int,
        state: PortfolioState,
        kelly: Optional[KellyEstimate],
        result: BacktestResult,
        costs: Costs,
    ) -> Optional[Trade]:
        entry_bar = signal_index + 1
        if entry_bar > len(bars) - 1:
            return None

        slippage = costs.slippage
        commission = costs.commission
        direction = 1 if proposal.side == Side.LONG else -1

        # Fill at the next bar's open, paying slippage in the wrong direction.
        raw_entry = bars[entry_bar].open
        fill_entry = raw_entry * (1.0 + direction * slippage)

        stop, target = proposal.stop, proposal.target

        # The stop was set against the signal bar's close. If the market gapped
        # past it overnight, the trade is already invalid and never opens.
        if direction > 0 and fill_entry <= stop:
            result.skipped_gap_through_stop += 1
            return None
        if direction < 0 and fill_entry >= stop:
            result.skipped_gap_through_stop += 1
            return None

        # Size against the real fill, not the hoped-for entry.
        priced = Proposal(
            sender=proposal.sender,
            book=proposal.book,
            instrument=proposal.instrument,
            side=proposal.side,
            strategy=proposal.strategy,
            entry=fill_entry,
            stop=stop,
            target=target,
            timeframe=proposal.timeframe,
            thesis=proposal.thesis,
            bear_case=proposal.bear_case,
            invalidation=proposal.invalidation,
        )
        sizing = size_proposal(priced, self.config, state, kelly)
        if not sizing.is_tradeable:
            result.skipped_no_size += 1
            return None
        quantity = sizing.quantity

        mae = 0.0
        mfe = 0.0
        exit_price: Optional[float] = None
        exit_reason = "end_of_data"
        exit_index = len(bars) - 1

        for j in range(entry_bar, len(bars)):
            bar = bars[j]
            excursion_low = (bar.low - fill_entry) * direction
            excursion_high = (bar.high - fill_entry) * direction
            mae = min(mae, excursion_low)
            mfe = max(mfe, excursion_high)

            hit_stop, hit_target = self._touches(bar, direction, stop, target)

            if hit_stop:
                # Ambiguity inside a bar resolves against the position, and a
                # gap through the level fills at the open, not at the level.
                gapped = (bar.open <= stop) if direction > 0 else (bar.open >= stop)
                level = bar.open if gapped else stop
                exit_price = level * (1.0 - direction * slippage)
                exit_reason = "stop"
                exit_index = j
                break

            if hit_target:
                gapped = (bar.open >= target) if direction > 0 else (bar.open <= target)
                # A limit fills at its level, or better on a favourable gap.
                exit_price = bar.open if gapped else target
                exit_reason = "target"
                exit_index = j
                break

            if self.max_holding_bars is not None and (j - entry_bar) >= self.max_holding_bars:
                exit_price = bar.close * (1.0 - direction * slippage)
                exit_reason = "timeout"
                exit_index = j
                break

        if exit_price is None:
            exit_price = bars[-1].close * (1.0 - direction * slippage)
            exit_reason = "end_of_data"
            exit_index = len(bars) - 1

        gross = (exit_price - fill_entry) * quantity * direction
        fees = (abs(quantity) * fill_entry + abs(quantity) * exit_price) * commission
        net = gross - fees

        return Trade(
            instrument=proposal.instrument,
            strategy=proposal.strategy,
            side=proposal.side,
            entry_ts=bars[entry_bar].ts,
            exit_ts=bars[exit_index].ts,
            entry=fill_entry,
            exit=exit_price,
            stop=stop,
            target=target,
            quantity=quantity,
            gross_pnl=gross,
            fees=fees,
            net_pnl=net,
            bars_held=exit_index - entry_bar,
            exit_reason=exit_reason,
            entry_bar=entry_bar,
            mae=mae,
            mfe=mfe,
        )

    @staticmethod
    def _touches(bar: Bar, direction: int, stop: float, target: float) -> tuple:
        if direction > 0:
            return bar.low <= stop, bar.high >= target
        return bar.high >= stop, bar.low <= target


__all__ = ["BacktestEngine", "BacktestResult", "Trade"]
