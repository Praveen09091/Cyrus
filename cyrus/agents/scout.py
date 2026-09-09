"""Scout: recon on crypto and the social tape.

Scout's job is asymmetric. Finding something that is moving is the easy half.
The half that keeps the desk alive is screening out what cannot be exited, so
liquidity is checked before anything is proposable and memecoins are flagged
rather than celebrated.

Every social read is an ASSUMPTION unless it cites a file in raw/. Scout is
structurally forbidden from turning a screenshot into evidence.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from cyrus.agents.base import Agent, Authority, SeatClass, SeatContext
from cyrus.bus.messages import (
    Evidence,
    Observation,
    Proposal,
    Stance,
    Vote,
)
from cyrus.config import DeskConfig
from cyrus.data.sources import SourceRegistry
from cyrus.indicators.core import (
    closes,
    last_valid,
    realized_volatility,
    volume_ratio,
    zscore,
)

_MEMECOINS = {"DOGE", "SHIB", "PEPE", "BONK", "WIF", "FLOKI", "TRUMP", "MOG"}


class Scout(Agent):
    name = "scout"
    seat_class = SeatClass.INTELLIGENCE
    authority = Authority.PROPOSE
    llm = "optional"

    def __init__(self, ctx: SeatContext, sources: SourceRegistry, config: DeskConfig) -> None:
        self.sources = sources
        self.config = config
        self.liquidity: Dict[str, Optional[float]] = {}
        self.flags: Dict[str, List[str]] = {}
        super().__init__(ctx)

    def register(self) -> None:
        self.bus.subscribe(Proposal, self.on_proposal)

    # --- recon ------------------------------------------------------------

    def scan(self, instruments: List[str], timeframe: str) -> List[Observation]:
        """Surface unusual activity and record a liquidity verdict per name."""
        out: List[Observation] = []
        for instrument in instruments:
            bars = self.sources.fetch(instrument, timeframe, 200)
            if len(bars) < 40:
                self.liquidity[instrument] = None
                continue

            source = self.sources.get(self.sources.last_used.get(instrument, ""))
            dollar_volume = source.dollar_volume(bars) if source else None
            self.liquidity[instrument] = dollar_volume

            price_series = closes(bars)
            vol_ratio = last_valid(volume_ratio(bars, 20))
            z = last_valid(zscore(price_series, 20))
            vol = realized_volatility(price_series, 20)

            flags: List[str] = []
            if _is_memecoin(instrument):
                flags.append("memecoin")
            floor = self.config.liquidity.min_avg_dollar_volume
            if dollar_volume is not None and dollar_volume < floor:
                flags.append("below_liquidity_floor")
            if dollar_volume is None:
                flags.append("liquidity_unknown")
            if vol_ratio is not None and vol_ratio >= 2.0:
                flags.append("volume_surge")
            self.flags[instrument] = flags

            provenance = self.sources.provenance(instrument)
            fallback = (
                "%s: volume %sx average, %s standard deviations from its 20-bar mean, "
                "realised vol %s. Average dollar volume %s. Flags: %s. Source: %s."
                % (
                    instrument,
                    _fmt(vol_ratio),
                    _fmt(z),
                    _fmt(vol),
                    _fmt_money(dollar_volume),
                    ", ".join(flags) or "none",
                    provenance,
                )
            )

            observation = Observation(
                sender=self.name,
                subject=instrument,
                statement=self.narrate(
                    "scout_scan",
                    {
                        "instrument": instrument,
                        "volume_ratio": vol_ratio,
                        "z": z,
                        "flags": flags,
                    },
                    fallback,
                ),
                evidence=Evidence.SOURCED if "none" not in provenance else Evidence.ASSUMPTION,
                confidence=0.5 if dollar_volume is not None else 0.2,
                sources=[provenance],
                data={
                    "volume_ratio": vol_ratio if vol_ratio is not None else -1.0,
                    "zscore": z if z is not None else 0.0,
                    "realized_vol": vol if vol is not None else -1.0,
                    "avg_dollar_volume": dollar_volume if dollar_volume is not None else -1.0,
                    "flags": flags,
                },
            )
            self.emit(observation)
            out.append(observation)
        return out

    def dollar_volume_for(self, instrument: str) -> Optional[float]:
        """Handed to the risk kernel as the liquidity input."""
        return self.liquidity.get(instrument)

    # --- voting -----------------------------------------------------------

    def on_proposal(self, proposal: Proposal) -> None:
        flags = self.flags.get(proposal.instrument, [])
        dollar_volume = self.liquidity.get(proposal.instrument)

        if "memecoin" in flags:
            self.emit(self._vote(
                proposal,
                Stance.REJECT,
                "Memecoin. Exit liquidity is unreliable and the rule requires a human stamp.",
                0.8,
            ))
            return

        if "below_liquidity_floor" in flags:
            self.emit(self._vote(
                proposal,
                Stance.REJECT,
                "Average dollar volume %s is under the %s floor. A position here cannot be exited cleanly."
                % (_fmt_money(dollar_volume), _fmt_money(self.config.liquidity.min_avg_dollar_volume)),
                0.8,
            ))
            return

        if dollar_volume is None:
            self.emit(self._vote(
                proposal,
                Stance.ABSTAIN,
                "No liquidity data for this name this cycle. Abstaining rather than guessing.",
                0.0,
            ))
            return

        self.emit(self._vote(
            proposal,
            Stance.APPROVE,
            "Liquidity clears the floor at %s average dollar volume." % _fmt_money(dollar_volume),
            0.5,
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


def _is_memecoin(instrument: str) -> bool:
    upper = instrument.upper().replace("-USD", "").replace("USDT", "")
    return upper in _MEMECOINS


def _fmt(value: Optional[float]) -> str:
    return "n/a" if value is None else "%.2f" % value


def _fmt_money(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    if value >= 1e9:
        return "$%.1fB" % (value / 1e9)
    if value >= 1e6:
        return "$%.1fM" % (value / 1e6)
    return "$%.0f" % value


__all__ = ["Scout"]
