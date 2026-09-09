"""Chartist: owns price structure.

Runs the book's strategy over the bars and publishes proposals. The indicator
math is deterministic; only the narration is ever a model's work.

Chartist never proposes without a stop, and every proposal carries the bear
case in the same message as the bull case. A one-sided idea is not a trade
idea, it is advertising.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from cyrus.agents.base import Agent, Authority, SeatClass, SeatContext
from cyrus.bus.messages import Proposal, Stance, Vote
from cyrus.config import BookConfig
from cyrus.data.sources import SourceRegistry
from cyrus.indicators.core import Bar, atr, closes, last_valid, macd, rsi
from cyrus.signals import for_style
from cyrus.signals.base import StrategyContext


class Chartist(Agent):
    name = "chartist"
    seat_class = SeatClass.ANALYSIS
    authority = Authority.PROPOSE
    llm = "optional"

    def __init__(self, ctx: SeatContext, sources: SourceRegistry) -> None:
        self.sources = sources
        self.bars_cache: Dict[str, List[Bar]] = {}
        self.context: Dict[str, Dict[str, float]] = {}
        super().__init__(ctx)

    def register(self) -> None:
        # Chartist votes on other seats' proposals, not on its own.
        self.bus.subscribe(Proposal, self.on_proposal)

    def analyze_book(self, book: BookConfig, regime: str = "unknown") -> List[Proposal]:
        """Run the book's strategy across its instruments."""
        strategy = for_style(book.style)
        proposals: List[Proposal] = []

        for instrument in book.instruments:
            bars = self.sources.fetch(instrument, book.timeframe, 400)
            if not bars:
                continue
            self.bars_cache[instrument] = bars
            self.context[instrument] = self._structure(bars)

            proposal = strategy(
                StrategyContext(
                    book=book.name,
                    instrument=instrument,
                    timeframe=book.timeframe,
                    bars=bars,
                    params=book.params,
                    cycle_id=self.ctx.cycle_id,
                    regime=regime,
                )
            )
            if proposal is None:
                continue

            proposal.sources = [self.sources.provenance(instrument)]
            self.emit(proposal)
            proposals.append(proposal)

        return proposals

    def bars_for(self, instrument: str) -> List[Bar]:
        return self.bars_cache.get(instrument, [])

    def _structure(self, bars: List[Bar]) -> Dict[str, float]:
        """Context indicators the desk quotes but does not trade on alone."""
        price_series = closes(bars)
        macd_line, signal_line, hist = macd(price_series)
        return {
            "close": price_series[-1],
            "atr": last_valid(atr(bars, 14)) or 0.0,
            "rsi": last_valid(rsi(price_series, 14)) or -1.0,
            "macd_hist": last_valid(hist) or 0.0,
        }

    # --- voting -----------------------------------------------------------

    def on_proposal(self, proposal: Proposal) -> None:
        """Structural review: is the stop somewhere a chart would put it?"""
        if proposal.sender == self.name:
            return

        context = self.context.get(proposal.instrument)
        if not context:
            self.emit(self._vote(proposal, Stance.ABSTAIN, "No chart context loaded.", 0.0))
            return

        atr_now = context.get("atr", 0.0)
        if atr_now <= 0:
            self.emit(self._vote(proposal, Stance.ABSTAIN, "No ATR available.", 0.0))
            return

        stop_in_atr = proposal.risk_per_unit / atr_now
        if stop_in_atr < 0.75:
            self.emit(self._vote(
                proposal,
                Stance.REJECT,
                "Stop is only %.2f ATR from entry. Normal noise will take it out." % stop_in_atr,
                0.7,
            ))
            return
        if stop_in_atr > 6.0:
            self.emit(self._vote(
                proposal,
                Stance.REJECT,
                "Stop is %.2f ATR away. The loss when wrong is larger than the structure justifies."
                % stop_in_atr,
                0.6,
            ))
            return

        self.emit(self._vote(
            proposal,
            Stance.APPROVE,
            "Stop sits %.2f ATR from entry, inside the range this structure supports." % stop_in_atr,
            0.55,
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


__all__ = ["Chartist"]
