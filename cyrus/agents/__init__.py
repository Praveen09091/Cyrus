"""The ten seats."""

from cyrus.agents.athena import Athena, CalendarEvent
from cyrus.agents.atlas import Atlas
from cyrus.agents.base import Agent, Authority, SeatClass, SeatContext
from cyrus.agents.chartist import Chartist
from cyrus.agents.ledger import ClosedTrade, Ledger
from cyrus.agents.oracle import Oracle, StrategyRecord
from cyrus.agents.orchestrator import Cyrus, CycleResult
from cyrus.agents.pilot import Pilot
from cyrus.agents.quartermaster import BurnReport, Quartermaster
from cyrus.agents.scout import Scout
from cyrus.agents.sentinel import Sentinel

__all__ = [
    "Agent",
    "Authority",
    "SeatClass",
    "SeatContext",
    "Atlas",
    "Scout",
    "Athena",
    "CalendarEvent",
    "Chartist",
    "Oracle",
    "StrategyRecord",
    "Sentinel",
    "Pilot",
    "Ledger",
    "ClosedTrade",
    "Quartermaster",
    "BurnReport",
    "Cyrus",
    "CycleResult",
]
