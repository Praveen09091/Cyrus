"""Atlas: macro weather.

Reads the tide every book floats on. Atlas does not pick trades. It classifies
the regime and tells the desk which styles historically fail in it, which is
how a mean-reversion book avoids fading the opening leg of a crash.

Regime is computed from price data, not from a narrative. VIX level and trend,
index trend, and the dollar decide it. Atlas votes against proposals whose
style contradicts the regime.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from cyrus.agents.base import Agent, Authority, SeatClass, SeatContext
from cyrus.bus.messages import (
    Evidence,
    Observation,
    Proposal,
    Regime,
    Side,
    Stance,
    Vote,
)
from cyrus.data.sources import SourceRegistry
from cyrus.indicators.core import atr, closes, ema, last_valid, realized_volatility

# Styles that historically bleed in a given regime.
_STYLE_CONFLICTS: Dict[Regime, List[str]] = {
    Regime.STRESS: ["mean_reversion", "momentum"],
    Regime.RISK_OFF: ["mean_reversion"],
    Regime.CHOP: ["trend", "momentum"],
}


class Atlas(Agent):
    name = "atlas"
    seat_class = SeatClass.INTELLIGENCE
    authority = Authority.PROPOSE
    llm = "optional"

    watch = ["^VIX", "SPY", "DX-Y.NYB"]

    def __init__(self, ctx: SeatContext, sources: SourceRegistry) -> None:
        self.sources = sources
        self.regime: Regime = Regime.UNKNOWN
        self.detail: Dict[str, float] = {}
        super().__init__(ctx)

    def register(self) -> None:
        self.bus.subscribe(Proposal, self.on_proposal)

    # --- regime ----------------------------------------------------------

    def read_regime(self) -> Observation:
        """Classify the tide and publish it as an observation."""
        vix_bars = self.sources.fetch("^VIX", "1d", 120)
        spy_bars = self.sources.fetch("SPY", "1d", 250)

        vix_level: Optional[float] = None
        vix_trend: Optional[float] = None
        if len(vix_bars) >= 40:
            vix_closes = closes(vix_bars)
            vix_level = vix_closes[-1]
            vix_ema = last_valid(ema(vix_closes, 20))
            if vix_ema:
                vix_trend = vix_level / vix_ema - 1.0

        spy_trend: Optional[float] = None
        spy_vol: Optional[float] = None
        if len(spy_bars) >= 210:
            spy_closes = closes(spy_bars)
            fast = last_valid(ema(spy_closes, 50))
            slow = last_valid(ema(spy_closes, 200))
            if fast and slow:
                spy_trend = fast / slow - 1.0
            spy_vol = realized_volatility(spy_closes, 20)
            atr_now = last_valid(atr(spy_bars, 14))
            if atr_now:
                self.detail["spy_atr"] = round(atr_now, 4)

        regime = self._classify(vix_level, vix_trend, spy_trend, spy_vol)
        self.regime = regime
        self.detail.update(
            {
                "vix": round(vix_level, 2) if vix_level is not None else -1.0,
                "vix_vs_ema20": round(vix_trend, 4) if vix_trend is not None else 0.0,
                "spy_50_200": round(spy_trend, 4) if spy_trend is not None else 0.0,
                "spy_realized_vol": round(spy_vol, 4) if spy_vol is not None else -1.0,
            }
        )

        provenance = self.sources.provenance("SPY")
        fallback = (
            "Regime is %s. VIX %s, SPY 50/200 spread %s, realised vol %s. Source: %s."
            % (
                regime.value,
                self.detail["vix"],
                self.detail["spy_50_200"],
                self.detail["spy_realized_vol"],
                provenance,
            )
        )
        statement = self.narrate("atlas_regime", dict(self.detail, regime=regime.value), fallback)

        observation = Observation(
            sender=self.name,
            subject="macro",
            statement=statement,
            evidence=Evidence.SOURCED if "none" not in provenance else Evidence.ASSUMPTION,
            confidence=self._confidence(vix_level, spy_trend),
            regime=regime,
            sources=[provenance],
            data=dict(self.detail),
        )
        self.emit(observation)
        return observation

    def _classify(
        self,
        vix_level: Optional[float],
        vix_trend: Optional[float],
        spy_trend: Optional[float],
        spy_vol: Optional[float],
    ) -> Regime:
        if vix_level is None and spy_trend is None:
            return Regime.UNKNOWN

        # Stress first: a spiking VIX invalidates almost every retail style.
        if vix_level is not None and vix_level >= 30.0:
            return Regime.STRESS
        if vix_level is not None and vix_level >= 22.0 and (vix_trend or 0.0) > 0.15:
            return Regime.STRESS

        if spy_trend is not None:
            if spy_trend > 0.02 and (vix_level is None or vix_level < 20.0):
                return Regime.RISK_ON
            if spy_trend < -0.02:
                return Regime.RISK_OFF

        # Flat trend with unremarkable vol is chop, where trend styles bleed.
        if spy_vol is not None and spy_vol < 0.14:
            return Regime.CHOP
        return Regime.CHOP

    def _confidence(self, vix_level: Optional[float], spy_trend: Optional[float]) -> float:
        known = sum(1 for v in (vix_level, spy_trend) if v is not None)
        return {0: 0.0, 1: 0.4, 2: 0.7}[known]

    # --- voting ----------------------------------------------------------

    def on_proposal(self, proposal: Proposal) -> None:
        """Vote from the regime, not from an opinion about the ticker."""
        conflicts = _STYLE_CONFLICTS.get(self.regime, [])
        strategy_style = _style_of(proposal.strategy)

        if self.regime == Regime.UNKNOWN:
            self.emit(
                Vote(
                    sender=self.name,
                    cycle_id=proposal.cycle_id,
                    correlation_id=proposal.correlation_id,
                    proposal_id=proposal.message_id,
                    stance=Stance.ABSTAIN,
                    reason="Regime unknown: no macro data this cycle.",
                    confidence=0.0,
                )
            )
            return

        if strategy_style in conflicts:
            self.emit(
                Vote(
                    sender=self.name,
                    cycle_id=proposal.cycle_id,
                    correlation_id=proposal.correlation_id,
                    proposal_id=proposal.message_id,
                    stance=Stance.REJECT,
                    reason="%s style historically bleeds in a %s regime."
                    % (strategy_style, self.regime.value),
                    confidence=0.6,
                )
            )
            return

        # Shorting risk assets into a risk-on tide is fighting the tape.
        if self.regime == Regime.RISK_ON and proposal.side == Side.SHORT:
            self.emit(
                Vote(
                    sender=self.name,
                    cycle_id=proposal.cycle_id,
                    correlation_id=proposal.correlation_id,
                    proposal_id=proposal.message_id,
                    stance=Stance.REJECT,
                    reason="Short into a risk-on regime fights the prevailing drift.",
                    confidence=0.5,
                )
            )
            return

        self.emit(
            Vote(
                sender=self.name,
                cycle_id=proposal.cycle_id,
                correlation_id=proposal.correlation_id,
                proposal_id=proposal.message_id,
                stance=Stance.APPROVE,
                reason="Regime %s does not contradict a %s entry."
                % (self.regime.value, strategy_style),
                confidence=0.55,
            )
        )

    def heartbeat(self) -> None:
        self.read_regime()


def _style_of(strategy_name: str) -> str:
    if "mean_reversion" in strategy_name:
        return "mean_reversion"
    if "momentum" in strategy_name or "breakout" in strategy_name:
        return "momentum"
    if "trend" in strategy_name:
        return "trend"
    return "unknown"


__all__ = ["Atlas"]
