"""Portfolio state the risk kernel reasons over.

This is the desk's memory of what it currently owns and how the equity curve
has behaved. It is intentionally plain: no model touches it, and every field
the kernel needs to say "no" is computed here rather than inferred at the
decision site.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from cyrus.bus.messages import Side


@dataclass
class Position:
    instrument: str
    book: str
    side: Side
    quantity: float
    entry: float
    stop: float
    opened_ts: float = field(default_factory=time.time)

    @property
    def notional(self) -> float:
        return abs(self.quantity) * self.entry

    @property
    def risk_usd(self) -> float:
        """Distance to the stop, which is the only loss the desk plans for."""
        return abs(self.quantity) * abs(self.entry - self.stop)

    @property
    def direction(self) -> int:
        return 1 if self.side == Side.LONG else -1


@dataclass
class PortfolioState:
    equity: float
    peak_equity: float = 0.0
    day_start_equity: float = 0.0
    positions: Dict[str, Position] = field(default_factory=dict)
    realized_pnl_today: float = 0.0
    halted: bool = False
    halt_reason: str = ""

    def __post_init__(self) -> None:
        if self.peak_equity <= 0:
            self.peak_equity = self.equity
        if self.day_start_equity <= 0:
            self.day_start_equity = self.equity

    # --- exposure ---------------------------------------------------------

    def open_count(self) -> int:
        return len(self.positions)

    def position(self, instrument: str) -> Optional[Position]:
        return self.positions.get(instrument)

    def book_positions(self, book: str) -> List[Position]:
        return [p for p in self.positions.values() if p.book == book]

    def book_risk_usd(self, book: str) -> float:
        return sum(p.risk_usd for p in self.book_positions(book))

    def book_notional(self, book: str) -> float:
        return sum(p.notional for p in self.book_positions(book))

    def total_risk_usd(self) -> float:
        return sum(p.risk_usd for p in self.positions.values())

    def factor_risk_usd(self, books: List[str], direction: Optional[int] = None) -> float:
        """Risk committed across a correlated group of books.

        When ``direction`` is given, only same-direction legs count: three longs
        across SPY, QQQ, and BTC are one bet, but a long and a short partially
        offset.
        """
        total = 0.0
        for position in self.positions.values():
            if position.book not in books:
                continue
            if direction is not None and position.direction != direction:
                continue
            total += position.risk_usd
        return total

    # --- equity health ----------------------------------------------------

    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity * 100.0)

    @property
    def day_loss_pct(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        change = self.day_start_equity - self.equity
        return max(0.0, change / self.day_start_equity * 100.0)

    # --- mutation ---------------------------------------------------------

    def mark_equity(self, equity: float) -> None:
        self.equity = equity
        self.peak_equity = max(self.peak_equity, equity)

    def open_position(self, position: Position) -> None:
        self.positions[position.instrument] = position

    def close_position(self, instrument: str, pnl: float = 0.0) -> Optional[Position]:
        position = self.positions.pop(instrument, None)
        if position is not None:
            self.realized_pnl_today += pnl
            self.mark_equity(self.equity + pnl)
        return position

    def start_new_day(self) -> None:
        self.day_start_equity = self.equity
        self.realized_pnl_today = 0.0
        # A daily halt clears with the day. A drawdown flatten does not.
        if self.halted and self.halt_reason == "daily_loss_halt":
            self.halted = False
            self.halt_reason = ""

    def halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason


__all__ = ["Position", "PortfolioState"]
