"""Position sizing.

The stop determines the size, never the other way around. Every book gets the
same dollar risk per trade regardless of how volatile its instrument is, which
is what keeps a quiet gold trade and a violent crypto trade comparable.

Sizing is arithmetic over the tightest binding cap. The cap that binds is
always named in the result, so a small size is explainable rather than
mysterious.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

from cyrus.bus.messages import Proposal, Side
from cyrus.config import DeskConfig
from cyrus.risk.kelly import KellyEstimate
from cyrus.risk.state import PortfolioState


@dataclass
class SizingResult:
    quantity: float
    notional: float
    risk_usd: float
    risk_pct: float
    binding_constraint: str
    caps: Dict[str, float] = field(default_factory=dict)
    note: str = ""

    @property
    def is_tradeable(self) -> bool:
        return self.quantity > 0.0


def size_proposal(
    proposal: Proposal,
    config: DeskConfig,
    state: PortfolioState,
    kelly: Optional[KellyEstimate] = None,
) -> SizingResult:
    """Convert a proposal plus an edge estimate into a quantity.

    Caps considered, all in dollars of risk:
      - per-trade cap from desk risk config
      - book cap
      - remaining factor budget for correlated books
      - Kelly's own recommendation, already fractional and sample-discounted
    """
    equity = config.equity
    risk_per_unit = proposal.risk_per_unit
    if equity <= 0 or risk_per_unit <= 0:
        return SizingResult(0.0, 0.0, 0.0, 0.0, "invalid_inputs", {}, "No equity or no stop distance.")

    book = config.book(proposal.book)
    caps: Dict[str, float] = {}

    caps["per_trade"] = equity * config.risk.max_risk_per_trade_pct / 100.0
    if book is not None:
        book_cap_total = equity * book.max_risk_pct / 100.0
        caps["book_remaining"] = max(0.0, book_cap_total - state.book_risk_usd(proposal.book))

    factor = config.factor_for_book(proposal.book)
    if factor is not None:
        factor_total = equity * factor.budget_pct / 100.0
        direction = 1 if proposal.side == Side.LONG else -1
        used = state.factor_risk_usd(factor.books, direction=direction)
        caps["factor_%s_remaining" % factor.name] = max(0.0, factor_total - used)

    if kelly is not None:
        # Kelly is a fraction of equity to put at risk, not a notional weight.
        caps["kelly"] = max(0.0, equity * kelly.applied)

    if _is_memecoin(proposal.instrument):
        caps["memecoin"] = equity * config.liquidity.memecoin_max_risk_pct / 100.0

    binding_name = min(caps, key=lambda k: caps[k]) if caps else "none"
    allowed_risk = caps.get(binding_name, 0.0)

    if allowed_risk <= 0.0:
        return SizingResult(
            0.0, 0.0, 0.0, 0.0, binding_name, caps,
            "Zero risk budget remaining under %s." % binding_name,
        )

    quantity = allowed_risk / risk_per_unit
    quantity = _round_quantity(quantity, proposal.instrument)
    if quantity <= 0.0:
        return SizingResult(
            0.0, 0.0, 0.0, 0.0, "min_lot", caps,
            "Allowed risk is smaller than one tradeable unit. Skip rather than oversize.",
        )

    risk_usd = quantity * risk_per_unit
    notional = quantity * proposal.entry
    note = "Binding cap: %s at $%.2f risk." % (binding_name, allowed_risk)
    if kelly is not None:
        note += " " + kelly.note

    return SizingResult(
        quantity=quantity,
        notional=notional,
        risk_usd=risk_usd,
        risk_pct=risk_usd / equity * 100.0,
        binding_constraint=binding_name,
        caps=caps,
        note=note,
    )


def _round_quantity(quantity: float, instrument: str) -> float:
    """Fractional units for crypto, whole shares for equities."""
    if _is_crypto(instrument):
        return float(int(quantity * 1e6)) / 1e6
    return float(int(quantity))


def _is_crypto(instrument: str) -> bool:
    upper = instrument.upper()
    return upper.endswith("-USD") or upper.endswith("USDT") or upper.endswith("-USDT")


def _is_memecoin(instrument: str) -> bool:
    """Conservative denylist. Unknown low-cap tokens should be added here.

    This is a floor, not a classifier. Scout still has to screen liquidity.
    """
    upper = instrument.upper().replace("-USD", "").replace("USDT", "")
    return upper in {"DOGE", "SHIB", "PEPE", "BONK", "WIF", "FLOKI", "TRUMP", "MOG"}


__all__ = ["SizingResult", "size_proposal"]
