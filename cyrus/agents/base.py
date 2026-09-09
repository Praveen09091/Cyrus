"""Agent base class and the authority model.

Authority is structural, not advisory. A seat with ``PROPOSE`` authority has no
code path to an order: it can only publish envelopes on the bus. Only the risk
seat issues authorisations, and only the execution seat consumes them.

Every seat is also usable without an LLM. The deterministic path always works;
a language model is a narrator and a synthesiser bolted on top, never the thing
holding the risk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from cyrus.bus.bus import Bus
from cyrus.bus.messages import Alert, AlertLevel, Envelope


class Authority(str, Enum):
    DECIDE = "decide"
    PROPOSE = "propose"
    VETO = "veto"
    EXECUTE_AUTHORIZED_ONLY = "execute_authorized_only"
    RECORD = "record"


class SeatClass(str, Enum):
    ORCHESTRATOR = "orchestrator"
    INTELLIGENCE = "intelligence"
    ANALYSIS = "analysis"
    RISK = "risk"
    EXECUTION = "execution"
    RECORD = "record"
    TREASURY = "treasury"


# A narrator turns a structured finding into prose. Optional by design: when it
# is absent, the seat emits the deterministic summary instead of going quiet.
Narrator = Callable[[str, Dict[str, Any]], str]


@dataclass
class SeatContext:
    """Shared handles a seat is given at construction."""

    bus: Bus
    cycle_id: str = ""
    narrator: Optional[Narrator] = None
    scratch: Dict[str, Any] = field(default_factory=dict)


class Agent:
    """One seat on the desk."""

    name = "agent"
    seat_class = SeatClass.INTELLIGENCE
    authority = Authority.PROPOSE
    llm = "optional"  # required | optional | forbidden

    def __init__(self, ctx: SeatContext) -> None:
        self.ctx = ctx
        self.bus = ctx.bus
        self.emitted: List[Envelope] = []
        self.register()

    # --- wiring -----------------------------------------------------------

    def register(self) -> None:
        """Subscribe to the message types this seat consumes."""
        return None

    def emit(self, message: Envelope) -> None:
        """Publish an envelope, stamping identity so chains stay traceable."""
        if not message.sender or message.sender == "unknown":
            message.sender = self.name
        if not message.cycle_id:
            message.cycle_id = self.ctx.cycle_id
        self._assert_authority(message)
        self.emitted.append(message)
        self.bus.publish(message)

    def _assert_authority(self, message: Envelope) -> None:
        """Structural guard: a proposing seat cannot fabricate an authorisation.

        This is a defensive check against a future edit, not against today's
        code. If it ever fires, the desk has a design bug and should stop.
        """
        kind = message.kind
        if kind == "Verdict" and self.authority != Authority.VETO:
            raise PermissionError(
                "%s (%s) may not issue a Verdict" % (self.name, self.authority.value)
            )
        if kind in ("OrderIntent", "Fill") and self.authority != Authority.EXECUTE_AUTHORIZED_ONLY:
            raise PermissionError(
                "%s (%s) may not emit %s" % (self.name, self.authority.value, kind)
            )
        if kind == "Decision" and self.authority != Authority.DECIDE:
            raise PermissionError(
                "%s (%s) may not record a Decision" % (self.name, self.authority.value)
            )

    # --- lifecycle --------------------------------------------------------

    def heartbeat(self) -> None:
        """Called on a schedule. Default is a no-op: silence is valid."""
        return None

    def narrate(self, template: str, payload: Dict[str, Any], fallback: str) -> str:
        """Prose via the narrator when available, deterministic text otherwise."""
        if self.ctx.narrator is None:
            return fallback
        try:
            text = self.ctx.narrator(template, payload)
        except Exception:
            return fallback
        return text.strip() or fallback

    def warn(self, code: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
        self.emit(
            Alert(
                sender=self.name,
                level=AlertLevel.WARN,
                code=code,
                message=message,
                data=data or {},
            )
        )

    def __repr__(self) -> str:
        return "<%s seat=%s authority=%s>" % (
            type(self).__name__,
            self.name,
            self.authority.value,
        )


__all__ = ["Agent", "Authority", "SeatClass", "SeatContext", "Narrator"]
