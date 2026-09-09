"""Pilot: places orders, and only authorised ones.

Pilot is the narrowest seat on the desk on purpose. It holds no opinion, runs
no model, and has exactly one question: does this intent carry a live
authorisation from the risk kernel? If not, it refuses.

The authorisation is single-use and time-limited. An idea cannot reach a venue
by being resubmitted, retried, or replayed.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from cyrus.agents.base import Agent, Authority, SeatClass, SeatContext
from cyrus.bus.messages import Alert, AlertLevel, Fill, OrderIntent, Proposal, Side, Verdict
from cyrus.execution.paper import Broker, client_order_id
from cyrus.risk.kernel import RiskKernel
from cyrus.risk.state import Position


class Pilot(Agent):
    name = "pilot"
    seat_class = SeatClass.EXECUTION
    authority = Authority.EXECUTE_AUTHORIZED_ONLY
    llm = "forbidden"

    def __init__(self, ctx: SeatContext, broker: Broker, kernel: RiskKernel) -> None:
        self.broker = broker
        self.kernel = kernel
        self.fills: List[Fill] = []
        super().__init__(ctx)

    def execute(self, verdict: Verdict, proposal: Proposal) -> Optional[Fill]:
        """Submit an approved order. Every refusal is an explicit alert."""
        if not verdict.approved:
            self._refuse(proposal, "Verdict is not an approval.")
            return None
        if verdict.proposal_id != proposal.message_id:
            self._refuse(proposal, "Authorisation does not match this proposal.")
            return None

        auth = self.kernel.consume(verdict.authorization_id or "")
        if auth is None:
            self._refuse(
                proposal,
                "Authorisation missing, expired, or already used. Nothing submitted.",
            )
            return None
        if auth.instrument != proposal.instrument or auth.side != proposal.side:
            self._refuse(proposal, "Authorisation is for a different instrument or side.")
            return None

        intent = OrderIntent(
            sender=self.name,
            cycle_id=proposal.cycle_id,
            correlation_id=proposal.correlation_id,
            proposal_id=proposal.message_id,
            authorization_id=auth.authorization_id,
            client_order_id=client_order_id(
                auth.authorization_id, proposal.instrument, auth.quantity
            ),
            instrument=proposal.instrument,
            book=proposal.book,
            side=proposal.side,
            quantity=auth.quantity,
            order_type="market",
            stop_price=proposal.stop,
            venue=self.broker.name,
        )
        self.emit(intent)

        fill = self.broker.submit(intent, reference_price=proposal.entry)
        fill.cycle_id = proposal.cycle_id
        self.emit(fill)
        self.fills.append(fill)

        if fill.status == "filled":
            self.kernel.state.open_position(
                Position(
                    instrument=proposal.instrument,
                    book=proposal.book,
                    side=proposal.side,
                    quantity=fill.quantity,
                    entry=fill.price,
                    stop=proposal.stop,
                )
            )
        return fill

    def flatten(self, reference_prices: Dict[str, float], reason: str) -> List[Fill]:
        """Close everything. Used by the drawdown switch.

        Flattening needs no authorisation: reducing risk is always permitted.
        Only opening or increasing risk requires a stamp.
        """
        fills: List[Fill] = []
        for instrument in list(self.kernel.state.positions.keys()):
            position = self.kernel.state.position(instrument)
            if position is None:
                continue
            exit_price = reference_prices.get(instrument, position.entry)
            closing_side = Side.SHORT if position.side == Side.LONG else Side.LONG

            intent = OrderIntent(
                sender=self.name,
                cycle_id=self.ctx.cycle_id,
                instrument=instrument,
                book=position.book,
                side=closing_side,
                quantity=abs(position.quantity),
                order_type="market",
                venue=self.broker.name,
                client_order_id=client_order_id(
                    "flatten_%s_%s" % (reason, instrument), instrument, position.quantity
                ),
            )
            self.emit(intent)

            fill = self.broker.submit(intent, reference_price=exit_price)
            self.emit(fill)
            fills.append(fill)

            pnl = (exit_price - position.entry) * position.quantity * position.direction
            self.kernel.state.close_position(instrument, pnl=pnl)

        if fills:
            self.emit(
                Alert(
                    sender=self.name,
                    level=AlertLevel.FLATTEN,
                    code="flattened",
                    message="Closed %d position(s): %s." % (len(fills), reason),
                    data={"reason": reason, "count": len(fills)},
                )
            )
        return fills

    def _refuse(self, proposal: Proposal, reason: str) -> None:
        self.emit(
            Alert(
                sender=self.name,
                level=AlertLevel.WARN,
                code="execution_refused",
                message="Refused to submit %s %s: %s"
                % (proposal.side.value, proposal.instrument, reason),
                data={"proposal_id": proposal.message_id, "instrument": proposal.instrument},
            )
        )


__all__ = ["Pilot"]
