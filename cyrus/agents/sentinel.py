"""Sentinel: the word no.

Sentinel is the only seat that can authorise money, and it does so through the
deterministic risk kernel. There is no model call anywhere on this path. Its
favourite answer is rejection, and a rejection is a successful outcome for the
desk, not a failure of the idea pipeline.

Sentinel reports to the human, not to Cyrus. Cyrus cannot overrule a veto.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from cyrus.agents.base import Agent, Authority, SeatClass, SeatContext
from cyrus.bus.messages import Alert, AlertLevel, Proposal, Verdict
from cyrus.risk.kelly import KellyEstimate
from cyrus.risk.kernel import RiskKernel


class Sentinel(Agent):
    name = "sentinel"
    seat_class = SeatClass.RISK
    authority = Authority.VETO
    llm = "forbidden"

    def __init__(self, ctx: SeatContext, kernel: RiskKernel) -> None:
        self.kernel = kernel
        self.verdicts: Dict[str, Verdict] = {}
        super().__init__(ctx)

    def review(
        self,
        proposal: Proposal,
        kelly: Optional[KellyEstimate] = None,
        approving_votes: int = 0,
        liquidity_dollar_volume: Optional[float] = None,
    ) -> Verdict:
        """Run the kernel and publish the ruling. Both outcomes are journaled."""
        verdict = self.kernel.review(
            proposal,
            kelly=kelly,
            approving_votes=approving_votes,
            liquidity_dollar_volume=liquidity_dollar_volume,
        )
        verdict.cycle_id = proposal.cycle_id
        self.verdicts[proposal.message_id] = verdict
        self.emit(verdict)
        return verdict

    def sweep(self) -> List[str]:
        """Kill switches, checked every cycle regardless of any proposal.

        Runs before ideas are considered, so a tripped switch stops the desk
        rather than racing a fresh proposal through the gate.
        """
        tripped = self.kernel.evaluate_kill_switches()
        for code in tripped:
            level = AlertLevel.FLATTEN if code == "drawdown_flatten" else AlertLevel.HALT
            self.emit(
                Alert(
                    sender=self.name,
                    level=level,
                    code=code,
                    message=self._explain(code),
                    data={
                        "equity": self.kernel.config.equity,
                        "drawdown_pct": round(self.kernel.state.drawdown_pct, 3),
                        "day_loss_pct": round(self.kernel.state.day_loss_pct, 3),
                        "open_positions": self.kernel.state.open_count(),
                    },
                )
            )
        return tripped

    def _explain(self, code: str) -> str:
        state = self.kernel.state
        risk = self.kernel.config.risk
        if code == "drawdown_flatten":
            return (
                "Drawdown of %.2f%% reached the %.2f%% flatten limit. Close every position "
                "and stop. The desk does not resume without a human review."
                % (state.drawdown_pct, risk.drawdown_flatten_pct)
            )
        if code == "daily_loss_halt":
            return (
                "Down %.2f%% today against a %.2f%% daily limit. No new positions until "
                "tomorrow. Existing stops stand."
                % (state.day_loss_pct, risk.daily_loss_halt_pct)
            )
        if code.startswith("burn_buffer"):
            burn = self.kernel.config.burn.monthly_total
            return (
                "Equity no longer covers %d months of the $%.2f monthly burn. Shrink size "
                "or idle. Trading harder to make it back is how the account dies."
                % (self.kernel.config.cash_buffer_months, burn)
            )
        return "Risk switch tripped: %s." % code

    def authorization_valid(self, authorization_id: str) -> bool:
        auth = self.kernel.authorizations.get(authorization_id)
        return auth is not None and auth.is_valid()


__all__ = ["Sentinel"]
