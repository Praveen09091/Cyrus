"""Desk assembly.

One function builds the whole firm and wires it to a bus, a config, a data
registry, and a broker. Everything is constructor-injected so a test can swap
the feed or the venue without touching a seat.

The default venue is paper, and selecting a live venue requires an explicit
config change plus a live equity figure. There is no code path that makes live
execution the accidental default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

from cyrus.agents.athena import Athena, CalendarEvent
from cyrus.agents.atlas import Atlas
from cyrus.agents.base import Narrator, SeatContext
from cyrus.agents.chartist import Chartist
from cyrus.agents.ledger import Ledger
from cyrus.agents.oracle import Oracle
from cyrus.agents.orchestrator import Cyrus, CycleResult
from cyrus.agents.pilot import Pilot
from cyrus.agents.quartermaster import Quartermaster
from cyrus.agents.scout import Scout
from cyrus.agents.sentinel import Sentinel
from cyrus.bus.bus import Bus, BusJournal
from cyrus.config import REPO_ROOT, DeskConfig, load_desk_config
from cyrus.data.sources import SourceRegistry, build_default_registry
from cyrus.execution.paper import Broker, PaperBroker
from cyrus.risk.kernel import RiskKernel
from cyrus.risk.state import PortfolioState


@dataclass
class Desk:
    """The assembled firm."""

    config: DeskConfig
    bus: Bus
    state: PortfolioState
    kernel: RiskKernel
    sources: SourceRegistry
    broker: Broker
    cyrus: Cyrus
    atlas: Atlas
    scout: Scout
    athena: Athena
    chartist: Chartist
    oracle: Oracle
    sentinel: Sentinel
    pilot: Pilot
    ledger: Ledger
    quartermaster: Quartermaster

    def run_cycle(self, book: str) -> CycleResult:
        return self.cyrus.run_cycle(book)

    def run_all_books(self) -> List[CycleResult]:
        return [self.run_cycle(book.name) for book in self.config.enabled_books()]

    def heartbeat(self) -> None:
        """One pass over every enabled book, then the treasury check."""
        self.run_all_books()
        self.quartermaster.report()

    def answer(self, question: str) -> str:
        return self.cyrus.answer(question)


def build_desk(
    config: Optional[DeskConfig] = None,
    ledger_dir: Optional[str] = None,
    sources: Optional[SourceRegistry] = None,
    broker: Optional[Broker] = None,
    narrator: Optional[Narrator] = None,
    calendar: Optional[List[CalendarEvent]] = None,
    news_available: bool = False,
    allow_network: bool = True,
) -> Desk:
    config = config or load_desk_config()
    ledger_dir = ledger_dir or os.path.join(REPO_ROOT, "ledger")

    bus = Bus(journal=BusJournal(os.path.join(ledger_dir, "bus")))
    state = PortfolioState(equity=config.equity)
    kernel = RiskKernel(config=config, state=state)
    sources = sources or build_default_registry(allow_network=allow_network)

    if broker is None:
        if config.execution.venue != "paper":
            raise ValueError(
                "Venue %r requires an explicit broker adapter. Live execution is never "
                "selected implicitly." % config.execution.venue
            )
        broker = PaperBroker(
            slippage_bps=config.execution.slippage_bps,
            commission_bps=config.execution.commission_bps,
        )
    if broker.is_live and not config.is_live:
        raise ValueError("A live broker was supplied while the desk mode is paper.")

    ctx = SeatContext(bus=bus, narrator=narrator)

    atlas = Atlas(SeatContext(bus=bus, narrator=narrator), sources)
    scout = Scout(SeatContext(bus=bus, narrator=narrator), sources, config)
    athena = Athena(
        SeatContext(bus=bus, narrator=narrator),
        calendar=calendar,
        news_available=news_available,
    )
    chartist = Chartist(SeatContext(bus=bus, narrator=narrator), sources)
    oracle = Oracle(SeatContext(bus=bus, narrator=narrator), config)
    sentinel = Sentinel(SeatContext(bus=bus), kernel)
    pilot = Pilot(SeatContext(bus=bus), broker, kernel)
    ledger = Ledger(SeatContext(bus=bus, narrator=narrator), ledger_dir)
    quartermaster = Quartermaster(SeatContext(bus=bus), config, state)

    cyrus = Cyrus(
        ctx,
        config=config,
        atlas=atlas,
        scout=scout,
        athena=athena,
        chartist=chartist,
        oracle=oracle,
        sentinel=sentinel,
        pilot=pilot,
        ledger=ledger,
        quartermaster=quartermaster,
    )

    return Desk(
        config=config,
        bus=bus,
        state=state,
        kernel=kernel,
        sources=sources,
        broker=broker,
        cyrus=cyrus,
        atlas=atlas,
        scout=scout,
        athena=athena,
        chartist=chartist,
        oracle=oracle,
        sentinel=sentinel,
        pilot=pilot,
        ledger=ledger,
        quartermaster=quartermaster,
    )


__all__ = ["Desk", "build_desk"]
