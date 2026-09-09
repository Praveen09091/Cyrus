"""Quartermaster: keeps Cyrus alive.

Tracks what the desk costs to run and whether trading is covering it. This is
the seat that enforces "pay for yourself or shrink", and its most common
recommendation is to do less.

The ladder is explicit: paper, micro live, hurdle, treasury, size-up. Each gate
must be passed, and a losing month cancels a size-up regardless of the average.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from cyrus.agents.base import Agent, Authority, SeatClass, SeatContext
from cyrus.bus.messages import Alert, AlertLevel, Evidence, Fill, Observation
from cyrus.config import DeskConfig
from cyrus.risk.state import PortfolioState

_LADDER = ["paper", "micro_live", "hurdle", "treasury", "size_up"]


@dataclass
class BurnReport:
    monthly_burn: float
    equity: float
    months_of_runway: float
    required_buffer: float
    covered: bool
    expectancy_90d: float
    hurdle: float
    hurdle_met: bool
    recommendation: str


class Quartermaster(Agent):
    name = "quartermaster"
    seat_class = SeatClass.TREASURY
    authority = Authority.RECORD
    llm = "forbidden"

    def __init__(self, ctx: SeatContext, config: DeskConfig, state: PortfolioState) -> None:
        self.config = config
        self.state = state
        self.realized_history: List[float] = []
        super().__init__(ctx)

    def register(self) -> None:
        self.bus.subscribe(Fill, self._on_fill)

    def _on_fill(self, fill: Fill) -> None:
        # Fees are a certainty and count against the burn immediately.
        if fill.fees:
            self.realized_history.append(-fill.fees)

    def record_realized(self, pnl: float) -> None:
        self.realized_history.append(pnl)

    # --- reporting --------------------------------------------------------

    def report(self) -> BurnReport:
        burn = self.config.burn.monthly_total
        equity = self.config.equity
        required = burn * self.config.cash_buffer_months
        runway = (equity / burn) if burn > 0 else float("inf")

        expectancy = sum(self.realized_history)
        hurdle = burn * self.config.burn.hurdle_multiple
        covered = equity >= required
        hurdle_met = expectancy >= hurdle

        report = BurnReport(
            monthly_burn=burn,
            equity=equity,
            months_of_runway=runway,
            required_buffer=required,
            covered=covered,
            expectancy_90d=expectancy,
            hurdle=hurdle,
            hurdle_met=hurdle_met,
            recommendation=self._recommend(covered, hurdle_met, expectancy, burn),
        )

        self.emit(
            Observation(
                sender=self.name,
                subject="treasury",
                statement=(
                    "Monthly burn $%.2f. Equity $%.2f is %.1f months of runway. "
                    "Realised PnL to date $%.2f against a $%.2f hurdle. %s"
                    % (burn, equity, runway, expectancy, hurdle, report.recommendation)
                ),
                evidence=Evidence.SOURCED,
                confidence=1.0,
                sources=["ledger", "desk_config"],
                data={
                    "monthly_burn": burn,
                    "equity": equity,
                    "months_of_runway": round(runway, 2),
                    "buffer_covered": covered,
                    "expectancy": expectancy,
                    "hurdle": hurdle,
                    "hurdle_met": hurdle_met,
                },
            )
        )

        if not covered:
            self.emit(
                Alert(
                    sender=self.name,
                    level=AlertLevel.HALT,
                    code="burn_buffer_breached",
                    message=(
                        "Equity $%.2f is below the $%.2f buffer needed for %d months of burn. "
                        "Shrink size or idle." % (equity, required, self.config.cash_buffer_months)
                    ),
                    data={"equity": equity, "required": required},
                )
            )

        return report

    def _recommend(self, covered: bool, hurdle_met: bool, expectancy: float, burn: float) -> str:
        if not covered:
            return "Below the cash buffer. Shrink size or idle until the buffer is restored."
        if self.config.mode == "paper":
            return (
                "Paper mode. No size-up is available until the loop has a realised record "
                "and a human moves the desk to micro live."
            )
        if expectancy <= 0:
            return "Realised expectancy is not positive. Hold size. Do not add risk to recover."
        if not hurdle_met:
            return (
                "Positive but below the hurdle. Hold current size; the desk is not yet paying "
                "for itself at %.0f%% of the target." % (expectancy / burn * 100.0 if burn else 0.0)
            )
        return "Hurdle met. A single size-up step is permitted after a human review."

    def gate(self, target_stage: str) -> Dict[str, Any]:
        """Is the desk allowed to advance to the next rung of the ladder?"""
        if target_stage not in _LADDER:
            return {"allowed": False, "reason": "unknown stage %r" % target_stage}
        report = self.report()
        if not report.covered:
            return {"allowed": False, "reason": "cash buffer not covered"}
        if target_stage in ("treasury", "size_up") and not report.hurdle_met:
            return {"allowed": False, "reason": "90-day expectancy below burn hurdle"}
        if target_stage != "paper" and self.state.halted:
            return {"allowed": False, "reason": "desk is halted: %s" % self.state.halt_reason}
        return {"allowed": True, "reason": "gates passed; human approval still required"}

    def heartbeat(self) -> None:
        self.report()


__all__ = ["Quartermaster", "BurnReport"]
