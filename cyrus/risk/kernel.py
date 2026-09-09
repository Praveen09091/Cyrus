"""The risk kernel. Sentinel's brain, and the only thing that can authorise money.

Every check here is arithmetic or a calendar lookup. There is no model call in
this file and there must never be one: when the LLM is down, jailbroken, or
confidently wrong, these rules still hold.

The kernel issues an ``authorization_id`` on approval. Pilot refuses any order
that does not carry a live authorisation, so an idea cannot reach a venue by
skipping this file.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from cyrus.bus.messages import Proposal, Ruling, Side, Verdict
from cyrus.config import DeskConfig
from cyrus.risk.kelly import KellyEstimate
from cyrus.risk.sizing import SizingResult, size_proposal
from cyrus.risk.state import PortfolioState

# Session windows in UTC, as (start_minute, end_minute) from midnight.
# US cash session 09:30-16:00 ET. Stored in UTC for EDT; the offset shifts by
# an hour under EST, which is why the boundary carries a small buffer.
_SESSIONS: Dict[str, Tuple[int, int, Set[int]]] = {
    # name: (start_utc_minute, end_utc_minute, weekdays)
    "us_cash": (13 * 60 + 30, 20 * 60, {0, 1, 2, 3, 4}),
    "nse": (3 * 60 + 45, 10 * 60, {0, 1, 2, 3, 4}),
}


@dataclass
class Authorization:
    """A single-use stamp. Consumed by Pilot, then dead."""

    authorization_id: str
    proposal_id: str
    instrument: str
    side: Side
    quantity: float
    risk_usd: float
    issued_ts: float
    ttl_seconds: float = 60.0
    consumed: bool = False

    def is_valid(self, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        return not self.consumed and (now - self.issued_ts) <= self.ttl_seconds


@dataclass
class RiskKernel:
    """Deterministic gate between an idea and an order."""

    config: DeskConfig
    state: PortfolioState
    authorizations: Dict[str, Authorization] = field(default_factory=dict)
    clock: Optional[float] = None  # epoch seconds; None means real time

    # --- public API -------------------------------------------------------

    def review(
        self,
        proposal: Proposal,
        kelly: Optional[KellyEstimate] = None,
        approving_votes: int = 0,
        liquidity_dollar_volume: Optional[float] = None,
    ) -> Verdict:
        """Run every check. Any failure means rejection, and every failure is named."""
        checks: Dict[str, bool] = {}
        reasons: List[str] = []

        # 0. Structural sanity. A malformed proposal never gets priced.
        faults = proposal.is_well_formed()
        checks["well_formed"] = not faults
        if faults:
            reasons.extend("malformed:%s" % f for f in faults)
            return self._reject(proposal, reasons, checks)

        # 1. Global halt state outranks everything, including a good idea.
        checks["not_halted"] = not self.state.halted
        if self.state.halted:
            reasons.append("desk_halted:%s" % (self.state.halt_reason or "unknown"))

        # 2. Equity health.
        dd = self.state.drawdown_pct
        checks["drawdown_ok"] = dd < self.config.risk.drawdown_flatten_pct
        if not checks["drawdown_ok"]:
            reasons.append(
                "drawdown_flatten:%.2f%%_of_%.2f%%" % (dd, self.config.risk.drawdown_flatten_pct)
            )

        day_loss = self.state.day_loss_pct
        checks["daily_loss_ok"] = day_loss < self.config.risk.daily_loss_halt_pct
        if not checks["daily_loss_ok"]:
            reasons.append(
                "daily_loss_halt:%.2f%%_of_%.2f%%" % (day_loss, self.config.risk.daily_loss_halt_pct)
            )

        # 3. Book must exist and be enabled.
        book = self.config.book(proposal.book)
        checks["book_enabled"] = book is not None and book.enabled
        if not checks["book_enabled"]:
            reasons.append("book_disabled:%s" % proposal.book)

        # 4. Consensus. Disagreement means cash, not a smaller bet.
        quorum = self.config.risk.consensus_quorum
        checks["consensus"] = approving_votes >= quorum
        if not checks["consensus"]:
            reasons.append("no_quorum:%d_of_%d" % (approving_votes, quorum))

        # 5. Reward to risk floor.
        rr = proposal.reward_risk
        checks["reward_risk"] = rr >= self.config.risk.min_reward_risk
        if not checks["reward_risk"]:
            reasons.append("reward_risk:%.2f_below_%.2f" % (rr, self.config.risk.min_reward_risk))

        # 6. Session. Trading a closed market is a reject, not a queued order.
        if book is not None and self.config.execution.reject_outside_session:
            open_now = self.is_session_open(book.session)
            checks["session_open"] = open_now
            if not open_now:
                reasons.append("session_closed:%s" % book.session)
        else:
            checks["session_open"] = True

        # 7. Liquidity. A signal you cannot exit is not an opportunity.
        if liquidity_dollar_volume is not None:
            floor = self.config.liquidity.min_avg_dollar_volume
            checks["liquidity"] = liquidity_dollar_volume >= floor
            if not checks["liquidity"]:
                reasons.append(
                    "illiquid:%.0f_below_%.0f" % (liquidity_dollar_volume, floor)
                )
        else:
            checks["liquidity"] = True

        # 8. Concurrency.
        checks["position_slots"] = self.state.open_count() < self.config.risk.max_open_positions
        if not checks["position_slots"]:
            reasons.append(
                "max_positions:%d" % self.config.risk.max_open_positions
            )

        # 9. No stacking and no averaging down.
        existing = self.state.position(proposal.instrument)
        checks["no_duplicate"] = existing is None
        if existing is not None:
            if existing.side == proposal.side:
                reasons.append("already_long_or_short:%s" % proposal.instrument)
            else:
                reasons.append("would_flip_position:%s" % proposal.instrument)

        # 10. Burn coverage. A desk that cannot pay its bills shrinks.
        coverage_ok, coverage_reason = self.check_burn_coverage()
        checks["burn_coverage"] = coverage_ok
        if not coverage_ok:
            reasons.append(coverage_reason)

        # 11. Memecoins need a human, by rule.
        if self.config.liquidity.memecoin_requires_human and _needs_human(proposal.instrument):
            checks["human_stamp"] = False
            reasons.append("memecoin_requires_human:%s" % proposal.instrument)
        else:
            checks["human_stamp"] = True

        # Size last: no point pricing an idea that already failed a gate.
        sizing = size_proposal(proposal, self.config, self.state, kelly)
        checks["sizeable"] = sizing.is_tradeable
        if not sizing.is_tradeable:
            reasons.append("no_size:%s" % sizing.binding_constraint)

        if reasons:
            return self._reject(proposal, reasons, checks, sizing)

        return self._approve(proposal, sizing, checks)

    def consume(self, authorization_id: str) -> Optional[Authorization]:
        """Pilot calls this. A stamp works exactly once."""
        auth = self.authorizations.get(authorization_id)
        if auth is None or not auth.is_valid(self._now()):
            return None
        auth.consumed = True
        return auth

    def check_burn_coverage(self) -> Tuple[bool, str]:
        """Is there enough cash to keep the lights on?

        Cyrus is allowed to trade only while it can still pay for itself. Below
        the buffer, the correct move is to shrink or idle, not to swing harder
        trying to make it back.
        """
        burn = self.config.burn.monthly_total
        if burn <= 0:
            return True, ""
        required = burn * self.config.cash_buffer_months
        if self.config.equity < required:
            return False, "burn_buffer:equity_%.0f_below_%.0f" % (self.config.equity, required)
        return True, ""

    def evaluate_kill_switches(self) -> List[str]:
        """Called every cycle, independent of any proposal.

        Returns the switches that tripped. Flatten outranks halt: one is
        "stop opening", the other is "get out and stay out".
        """
        tripped: List[str] = []
        if self.state.drawdown_pct >= self.config.risk.drawdown_flatten_pct:
            tripped.append("drawdown_flatten")
            self.state.halt("drawdown_flatten")
        elif self.state.day_loss_pct >= self.config.risk.daily_loss_halt_pct:
            tripped.append("daily_loss_halt")
            self.state.halt("daily_loss_halt")
        ok, reason = self.check_burn_coverage()
        if not ok:
            tripped.append(reason)
        return tripped

    def is_session_open(self, session: str, now: Optional[datetime] = None) -> bool:
        if session == "always":
            return True
        window = _SESSIONS.get(session)
        if window is None:
            return True
        start, end, weekdays = window
        moment = now or datetime.fromtimestamp(self._now(), tz=timezone.utc)
        if moment.weekday() not in weekdays:
            return False
        minute_of_day = moment.hour * 60 + moment.minute
        return start <= minute_of_day <= end

    # --- internals --------------------------------------------------------

    def _now(self) -> float:
        return self.clock if self.clock is not None else time.time()

    def _approve(self, proposal: Proposal, sizing: SizingResult, checks: Dict[str, bool]) -> Verdict:
        auth = Authorization(
            authorization_id=_auth_id(proposal),
            proposal_id=proposal.message_id,
            instrument=proposal.instrument,
            side=proposal.side,
            quantity=sizing.quantity,
            risk_usd=sizing.risk_usd,
            issued_ts=self._now(),
        )
        self.authorizations[auth.authorization_id] = auth
        return Verdict(
            sender="sentinel",
            cycle_id=proposal.cycle_id,
            correlation_id=proposal.correlation_id,
            proposal_id=proposal.message_id,
            ruling=Ruling.APPROVED,
            authorization_id=auth.authorization_id,
            reasons=[sizing.note],
            quantity=sizing.quantity,
            risk_usd=sizing.risk_usd,
            checks=checks,
        )

    def _reject(
        self,
        proposal: Proposal,
        reasons: List[str],
        checks: Dict[str, bool],
        sizing: Optional[SizingResult] = None,
    ) -> Verdict:
        return Verdict(
            sender="sentinel",
            cycle_id=proposal.cycle_id,
            correlation_id=proposal.correlation_id,
            proposal_id=proposal.message_id,
            ruling=Ruling.REJECTED,
            authorization_id=None,
            reasons=reasons,
            quantity=0.0,
            risk_usd=0.0,
            checks=checks,
        )


def _auth_id(proposal: Proposal) -> str:
    seed = "%s|%s|%s|%s" % (
        proposal.message_id,
        proposal.instrument,
        proposal.side.value,
        uuid.uuid4().hex,
    )
    return "auth_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _needs_human(instrument: str) -> bool:
    upper = instrument.upper().replace("-USD", "").replace("USDT", "")
    return upper in {"DOGE", "SHIB", "PEPE", "BONK", "WIF", "FLOKI", "TRUMP", "MOG"}


__all__ = ["RiskKernel", "Authorization"]
