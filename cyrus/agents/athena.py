"""Athena: news, filings, and the event calendar.

Athena answers one question the chart cannot: is this already priced in, and
is there a scheduled event about to invalidate the setup?

The event calendar is a hard blocker rather than a soft opinion. Holding a
mean-reversion position through an earnings print is not a trade, it is a coin
flip with a stop that will gap straight through.

Without a wired news feed Athena stays honest: it reports that it has no
catalyst data and abstains. An abstention is information. A fabricated
headline is the worst thing this repo could produce.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from cyrus.agents.base import Agent, Authority, SeatClass, SeatContext
from cyrus.bus.messages import Evidence, Observation, Proposal, Stance, Vote


@dataclass
class CalendarEvent:
    """A scheduled event that can gap a price."""

    instrument: str
    label: str
    ts: float
    blocks_entry: bool = True

    def hours_away(self, now: Optional[float] = None) -> float:
        now = now if now is not None else datetime.now(timezone.utc).timestamp()
        return (self.ts - now) / 3600.0


class Athena(Agent):
    name = "athena"
    seat_class = SeatClass.INTELLIGENCE
    authority = Authority.PROPOSE
    llm = "optional"

    # Entries inside this window are blocked outright.
    event_block_hours = 48.0

    def __init__(
        self,
        ctx: SeatContext,
        calendar: Optional[List[CalendarEvent]] = None,
        news_available: bool = False,
    ) -> None:
        self.calendar: List[CalendarEvent] = calendar or []
        self.news_available = news_available
        self.catalysts: Dict[str, str] = {}
        super().__init__(ctx)

    def register(self) -> None:
        self.bus.subscribe(Proposal, self.on_proposal)

    # --- research ---------------------------------------------------------

    def add_event(self, event: CalendarEvent) -> None:
        self.calendar.append(event)

    def upcoming(self, instrument: str, within_hours: Optional[float] = None) -> List[CalendarEvent]:
        limit = self.event_block_hours if within_hours is None else within_hours
        return [
            e
            for e in self.calendar
            if e.instrument.upper() == instrument.upper() and 0.0 <= e.hours_away() <= limit
        ]

    def brief(self, instruments: List[str]) -> List[Observation]:
        """Report catalysts and event risk, or report the absence of data."""
        out: List[Observation] = []
        for instrument in instruments:
            events = self.upcoming(instrument)
            catalyst = self.catalysts.get(instrument)

            if events:
                statement = "%s has %s inside the next %.0f hours: %s." % (
                    instrument,
                    "an event" if len(events) == 1 else "%d events" % len(events),
                    self.event_block_hours,
                    ", ".join("%s in %.0fh" % (e.label, e.hours_away()) for e in events),
                )
                evidence = Evidence.SOURCED
                confidence = 0.8
            elif catalyst:
                statement = "%s catalyst: %s." % (instrument, catalyst)
                evidence = Evidence.MIXED
                confidence = 0.5
            elif self.news_available:
                statement = "%s: no scheduled event and no catalyst found. Any move is flow, not news." % instrument
                evidence = Evidence.SOURCED
                confidence = 0.5
            else:
                statement = (
                    "%s: no news feed is wired, so Athena has no catalyst data. "
                    "Treat the absence of news as unknown, not as clear." % instrument
                )
                evidence = Evidence.ASSUMPTION
                confidence = 0.0

            observation = Observation(
                sender=self.name,
                subject=instrument,
                statement=statement,
                evidence=evidence,
                confidence=confidence,
                sources=["calendar"] if events else [],
                data={
                    "event_count": len(events),
                    "events": [e.label for e in events],
                    "news_feed": self.news_available,
                },
            )
            self.emit(observation)
            out.append(observation)
        return out

    # --- voting -----------------------------------------------------------

    def on_proposal(self, proposal: Proposal) -> None:
        events = [e for e in self.upcoming(proposal.instrument) if e.blocks_entry]
        if events:
            nearest = min(events, key=lambda e: e.hours_away())
            self.emit(self._vote(
                proposal,
                Stance.REJECT,
                "%s is %.0f hours away. A scheduled event can gap straight through the stop."
                % (nearest.label, nearest.hours_away()),
                0.85,
            ))
            return

        if not self.news_available:
            self.emit(self._vote(
                proposal,
                Stance.ABSTAIN,
                "No news feed wired. Athena cannot confirm or deny a catalyst, so it abstains.",
                0.0,
            ))
            return

        catalyst = self.catalysts.get(proposal.instrument)
        if catalyst:
            self.emit(self._vote(
                proposal,
                Stance.APPROVE,
                "Catalyst present and calendar is clear: %s." % catalyst,
                0.6,
            ))
            return

        self.emit(self._vote(
            proposal,
            Stance.APPROVE,
            "Calendar is clear for the next %.0f hours. No event risk against this entry."
            % self.event_block_hours,
            0.45,
        ))

    def _vote(self, proposal: Proposal, stance: Stance, reason: str, confidence: float) -> Vote:
        return Vote(
            sender=self.name,
            cycle_id=proposal.cycle_id,
            correlation_id=proposal.correlation_id,
            proposal_id=proposal.message_id,
            stance=stance,
            reason=reason,
            confidence=confidence,
        )


__all__ = ["Athena", "CalendarEvent"]
