"""Typed envelopes for every message that crosses the desk.

No seat reaches into another seat's internals. Everything travels as one of
these envelopes, carrying enough identity to reconstruct the full chain from
first observation to final fill.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


class Evidence(str, Enum):
    """Whether a claim is backed by a source in raw/ or is the model's read."""

    SOURCED = "sourced"
    MIXED = "mixed"
    ASSUMPTION = "assumption"


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class Stance(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    ABSTAIN = "abstain"


class Ruling(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class Regime(str, Enum):
    RISK_ON = "risk_on"
    RISK_OFF = "risk_off"
    CHOP = "chop"
    STRESS = "stress"
    UNKNOWN = "unknown"


class AlertLevel(str, Enum):
    INFO = "info"
    WARN = "warn"
    HALT = "halt"
    FLATTEN = "flatten"


def _new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, uuid.uuid4().hex[:12])


@dataclass
class Envelope:
    """Common identity carried by every message.

    ``correlation_id`` threads one idea across seats: the observation that
    started it, the proposal it became, the verdict, the order, the fill.
    """

    sender: str = "unknown"
    cycle_id: str = ""
    correlation_id: str = field(default_factory=lambda: _new_id("corr"))
    message_id: str = field(default_factory=lambda: _new_id("msg"))
    ts: float = field(default_factory=time.time)

    @property
    def kind(self) -> str:
        return type(self).__name__

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["kind"] = self.kind
        return _plain(out)


@dataclass
class ScanRequest(Envelope):
    """Cyrus opening a cycle and asking the intelligence seats to look."""

    book: str = ""
    instruments: List[str] = field(default_factory=list)
    timeframe: str = ""
    reason: str = ""


@dataclass
class Observation(Envelope):
    """A fact or a read. Never a trade instruction."""

    subject: str = ""
    statement: str = ""
    evidence: Evidence = Evidence.ASSUMPTION
    confidence: float = 0.0
    regime: Regime = Regime.UNKNOWN
    sources: List[str] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.evidence == Evidence.SOURCED and not self.sources:
            # A sourced claim without a citation is an assumption wearing a suit.
            self.evidence = Evidence.ASSUMPTION


@dataclass
class Proposal(Envelope):
    """A trade idea. Carries its own bear case and its own invalidation."""

    book: str = ""
    instrument: str = ""
    side: Side = Side.FLAT
    strategy: str = ""
    entry: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    timeframe: str = ""
    thesis: str = ""
    bear_case: str = ""
    invalidation: str = ""
    evidence: Evidence = Evidence.ASSUMPTION
    indicators: Dict[str, float] = field(default_factory=dict)
    sources: List[str] = field(default_factory=list)

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def reward_risk(self) -> float:
        risk = self.risk_per_unit
        if risk <= 0:
            return 0.0
        return abs(self.target - self.entry) / risk

    def faults(self) -> List[str]:
        """Structural faults. A proposal with faults never reaches sizing.

        Returns the empty list when the proposal is sound, so callers read as
        ``if proposal.faults(): reject``.
        """
        faults: List[str] = []
        if self.side not in (Side.LONG, Side.SHORT):
            faults.append("no_direction")
        if self.entry <= 0:
            faults.append("no_entry")
        if self.stop <= 0:
            faults.append("no_stop")
        if self.risk_per_unit <= 0:
            faults.append("stop_equals_entry")
        if self.side == Side.LONG and self.stop >= self.entry:
            faults.append("long_stop_above_entry")
        if self.side == Side.SHORT and self.stop <= self.entry:
            faults.append("short_stop_below_entry")
        if not self.bear_case.strip():
            faults.append("no_bear_case")
        if not self.invalidation.strip():
            faults.append("no_invalidation")
        return faults


@dataclass
class Vote(Envelope):
    """One seat's stance on a proposal. Quorum of these, or the answer is cash."""

    proposal_id: str = ""
    stance: Stance = Stance.ABSTAIN
    reason: str = ""
    confidence: float = 0.0


@dataclass
class SizeRequest(Envelope):
    """Oracle's estimate of the edge, handed to the risk kernel to convert."""

    proposal_id: str = ""
    win_rate: float = 0.0
    win_payoff: float = 0.0
    loss_payoff: float = 1.0
    sample_size: int = 0
    out_of_sample: bool = False
    note: str = ""


@dataclass
class SizeDecision(Envelope):
    """The arithmetic result. Every binding cap is named."""

    proposal_id: str = ""
    quantity: float = 0.0
    notional: float = 0.0
    risk_usd: float = 0.0
    risk_pct: float = 0.0
    kelly_raw: float = 0.0
    kelly_applied: float = 0.0
    binding_constraint: str = ""
    caps: Dict[str, float] = field(default_factory=dict)


@dataclass
class Verdict(Envelope):
    """Sentinel's ruling. Only an APPROVED verdict carries an authorization_id."""

    proposal_id: str = ""
    ruling: Ruling = Ruling.REJECTED
    authorization_id: Optional[str] = None
    reasons: List[str] = field(default_factory=list)
    quantity: float = 0.0
    risk_usd: float = 0.0
    checks: Dict[str, bool] = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.ruling == Ruling.APPROVED and bool(self.authorization_id)


@dataclass
class OrderIntent(Envelope):
    """What Pilot is about to submit. Rejected outright without authorisation."""

    proposal_id: str = ""
    authorization_id: str = ""
    client_order_id: str = ""
    instrument: str = ""
    book: str = ""
    side: Side = Side.FLAT
    quantity: float = 0.0
    order_type: str = "market"
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    venue: str = "paper"


@dataclass
class Fill(Envelope):
    """Execution result, reported verbatim. Partials and rejects included."""

    proposal_id: str = ""
    client_order_id: str = ""
    instrument: str = ""
    side: Side = Side.FLAT
    quantity: float = 0.0
    price: float = 0.0
    status: str = "filled"  # filled | partial | rejected | cancelled
    fees: float = 0.0
    slippage_bps: float = 0.0
    venue: str = "paper"
    note: str = ""


@dataclass
class Decision(Envelope):
    """Cyrus on the record: what the desk did, and why, in its own words."""

    book: str = ""
    action: str = ""  # trade | stand_down | flatten | idle
    proposal_id: Optional[str] = None
    rationale: str = ""
    dissent: List[str] = field(default_factory=list)
    votes: Dict[str, str] = field(default_factory=dict)
    accountable: str = "cyrus"


@dataclass
class Alert(Envelope):
    """Kill switch, drift, or burn tripwire. Never swallowed."""

    level: AlertLevel = AlertLevel.INFO
    code: str = ""
    message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LedgerEntry(Envelope):
    """Append-only record. Written by the record seat, never edited."""

    entry_type: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


def _plain(value: Any) -> Any:
    """Recursively convert enums to their values so records stay JSON-clean."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


__all__ = [
    "Evidence",
    "Side",
    "Stance",
    "Ruling",
    "Regime",
    "AlertLevel",
    "Envelope",
    "ScanRequest",
    "Observation",
    "Proposal",
    "Vote",
    "SizeRequest",
    "SizeDecision",
    "Verdict",
    "OrderIntent",
    "Fill",
    "Decision",
    "Alert",
    "LedgerEntry",
]
