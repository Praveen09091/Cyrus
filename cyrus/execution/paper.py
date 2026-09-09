"""Paper broker: the default venue.

Fills at the next available price with configured slippage and commission. It
is deliberately pessimistic: slippage always works against the order, because
a backtest that fills at the mid is a story about a market that does not exist.

This is the only venue enabled by default. A live adapter must implement the
same interface and must never be selected implicitly.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from cyrus.bus.messages import Fill, OrderIntent, Side


class Broker:
    """Venue interface. Live adapters implement exactly this."""

    name = "broker"
    is_live = False

    def submit(self, intent: OrderIntent, reference_price: float) -> Fill:
        raise NotImplementedError


@dataclass
class PaperBroker(Broker):
    name: str = "paper"
    is_live: bool = False
    slippage_bps: float = 5.0
    commission_bps: float = 0.0
    reject_zero_price: bool = True
    submitted: Dict[str, Fill] = field(default_factory=dict)
    log: List[Fill] = field(default_factory=list)

    def submit(self, intent: OrderIntent, reference_price: float) -> Fill:
        # Idempotency: the same client order id never fills twice. A retry after
        # a timeout must not double the position.
        if intent.client_order_id in self.submitted:
            existing = self.submitted[intent.client_order_id]
            return Fill(
                sender="pilot",
                cycle_id=intent.cycle_id,
                correlation_id=intent.correlation_id,
                proposal_id=intent.proposal_id,
                client_order_id=intent.client_order_id,
                instrument=intent.instrument,
                side=intent.side,
                quantity=existing.quantity,
                price=existing.price,
                status="duplicate",
                venue=self.name,
                note="Client order id already filled. Returning the original fill.",
            )

        if reference_price <= 0 and self.reject_zero_price:
            return self._reject(intent, "No reference price available.")
        if intent.quantity <= 0:
            return self._reject(intent, "Quantity is zero or negative.")

        # Slippage always against the order.
        direction = 1 if intent.side == Side.LONG else -1
        fill_price = reference_price * (1.0 + direction * self.slippage_bps / 10_000.0)
        fees = abs(intent.quantity) * fill_price * self.commission_bps / 10_000.0

        fill = Fill(
            sender="pilot",
            cycle_id=intent.cycle_id,
            correlation_id=intent.correlation_id,
            proposal_id=intent.proposal_id,
            client_order_id=intent.client_order_id,
            instrument=intent.instrument,
            side=intent.side,
            quantity=intent.quantity,
            price=round(fill_price, 8),
            status="filled",
            fees=round(fees, 6),
            slippage_bps=self.slippage_bps,
            venue=self.name,
            note="PAPER fill. No real money moved.",
        )
        self.submitted[intent.client_order_id] = fill
        self.log.append(fill)
        return fill

    def _reject(self, intent: OrderIntent, reason: str) -> Fill:
        fill = Fill(
            sender="pilot",
            cycle_id=intent.cycle_id,
            correlation_id=intent.correlation_id,
            proposal_id=intent.proposal_id,
            client_order_id=intent.client_order_id,
            instrument=intent.instrument,
            side=intent.side,
            quantity=0.0,
            price=0.0,
            status="rejected",
            venue=self.name,
            note=reason,
        )
        self.log.append(fill)
        return fill


def client_order_id(authorization_id: str, instrument: str, quantity: float) -> str:
    """Deterministic id so a retry is recognisable as the same order."""
    seed = "%s|%s|%.8f" % (authorization_id, instrument, quantity)
    return "cyrus_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


__all__ = ["Broker", "PaperBroker", "client_order_id"]
