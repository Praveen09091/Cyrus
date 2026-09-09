"""Oracle: the quant seat.

Answers in probabilities, never promises. Oracle's real product is a Kelly
fraction derived from the strategy's own recorded track record, and its most
valuable output is a zero: no history means no size.

Oracle also runs the spectral diagnostic. It treats a beautiful in-sample
cycle as a warning sign, and it will vote against a proposal whose only
support is a Fourier fit.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from cyrus.agents.base import Agent, Authority, SeatClass, SeatContext
from cyrus.bus.messages import (
    Evidence,
    Observation,
    Proposal,
    SizeRequest,
    Stance,
    Vote,
)
from cyrus.config import DeskConfig
from cyrus.indicators.core import Bar, atr, closes, last_valid
from cyrus.indicators.spectral import SpectralReport, analyze, cliff_cost
from cyrus.risk.kelly import KellyEstimate, estimate


class StrategyRecord:
    """Realised performance of one strategy, from the ledger.

    Zero trades means zero size. That is the point: a strategy earns the right
    to be sized by surviving, not by backtesting well.
    """

    def __init__(self, strategy: str) -> None:
        self.strategy = strategy
        self.wins = 0
        self.losses = 0
        self.gross_win = 0.0
        self.gross_loss = 0.0
        self.out_of_sample = False

    def record(self, pnl: float) -> None:
        if pnl >= 0:
            self.wins += 1
            self.gross_win += pnl
        else:
            self.losses += 1
            self.gross_loss += abs(pnl)

    @property
    def sample_size(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        if self.sample_size == 0:
            return 0.0
        return self.wins / self.sample_size

    @property
    def avg_win(self) -> float:
        return self.gross_win / self.wins if self.wins else 0.0

    @property
    def avg_loss(self) -> float:
        return self.gross_loss / self.losses if self.losses else 0.0


class Oracle(Agent):
    name = "oracle"
    seat_class = SeatClass.ANALYSIS
    authority = Authority.PROPOSE
    llm = "optional"

    def __init__(self, ctx: SeatContext, config: DeskConfig) -> None:
        self.config = config
        self.records: Dict[str, StrategyRecord] = {}
        self.spectral: Dict[str, SpectralReport] = {}
        super().__init__(ctx)

    def register(self) -> None:
        self.bus.subscribe(Proposal, self.on_proposal)

    # --- edge estimation --------------------------------------------------

    def record_outcome(self, strategy: str, pnl: float) -> None:
        self.records.setdefault(strategy, StrategyRecord(strategy)).record(pnl)

    def kelly_for(self, proposal: Proposal) -> KellyEstimate:
        """Kelly from realised results, or an explicit zero.

        Falls back to the proposal's own reward/risk only as the payoff shape,
        never as a substitute for a win rate. Without a win rate there is no
        edge estimate, and without an edge estimate there is no size.
        """
        record = self.records.get(proposal.strategy)
        if record is None or record.sample_size == 0:
            return estimate(
                win_rate=0.0,
                win_payoff=proposal.reward_risk or 1.0,
                loss_payoff=1.0,
                sample_size=0,
                out_of_sample=False,
                desk_fraction=self.config.risk.kelly_fraction,
            )

        win_payoff = record.avg_win or (proposal.reward_risk or 1.0)
        loss_payoff = record.avg_loss or 1.0
        return estimate(
            win_rate=record.win_rate,
            win_payoff=win_payoff,
            loss_payoff=loss_payoff,
            sample_size=record.sample_size,
            out_of_sample=record.out_of_sample,
            desk_fraction=self.config.risk.kelly_fraction,
        )

    def size_request(self, proposal: Proposal) -> SizeRequest:
        record = self.records.get(proposal.strategy)
        kelly = self.kelly_for(proposal)
        request = SizeRequest(
            sender=self.name,
            cycle_id=proposal.cycle_id,
            correlation_id=proposal.correlation_id,
            proposal_id=proposal.message_id,
            win_rate=record.win_rate if record else 0.0,
            win_payoff=record.avg_win if record else 0.0,
            loss_payoff=record.avg_loss if record else 1.0,
            sample_size=record.sample_size if record else 0,
            out_of_sample=record.out_of_sample if record else False,
            note=kelly.note,
        )
        self.emit(request)
        return request

    # --- spectral diagnostic ---------------------------------------------

    def spectral_report(self, instrument: str, bars: List[Bar]) -> Optional[Observation]:
        """Decompose the series and publish how much of it to believe.

        x(t) = sum_k A_k cos(2 pi f_k t + rho_k). Eight vectors, one tip
        redrawing the price. The number that matters is not the in-sample fit,
        it is what survives the holdout after the wrap cliff is killed.
        """
        if len(bars) < 64:
            return None

        price_series = closes(bars)
        atr_now = last_valid(atr(bars, 14))
        report = analyze(price_series, top_k=8, atr_value=atr_now)
        self.spectral[instrument] = report
        cliff = cliff_cost(price_series, top_k=8)

        statement = (
            "%s spectral read: %s Wrap cliff %.4f (%s leakage risk); killing the cliff "
            "removes %.0f%% of the dominant cycle's power, so that share was a transform "
            "artifact rather than a market cycle."
            % (
                instrument,
                report.verdict,
                report.wrap_cliff,
                report.leakage_risk,
                cliff["artifact_share"] * 100.0,
            )
        )

        observation = Observation(
            sender=self.name,
            subject=instrument,
            statement=statement,
            evidence=Evidence.MIXED,
            confidence=0.3 if not report.is_tradeable_evidence else 0.5,
            data={
                "dominant_period_samples": report.dominant_period_samples or 0.0,
                "dominant_power_fraction": report.dominant_power_fraction or 0.0,
                "in_sample_r2": report.in_sample_r2 or 0.0,
                "out_of_sample_r2": report.out_of_sample_r2 or 0.0,
                "wrap_cliff": report.wrap_cliff,
                "leakage_risk": report.leakage_risk,
                "artifact_share": cliff["artifact_share"],
                "tradeable_evidence": report.is_tradeable_evidence,
            },
        )
        self.emit(observation)
        return observation

    # --- voting -----------------------------------------------------------

    def on_proposal(self, proposal: Proposal) -> None:
        kelly = self.kelly_for(proposal)
        record = self.records.get(proposal.strategy)

        if record is None or record.sample_size == 0:
            self.emit(self._vote(
                proposal,
                Stance.ABSTAIN,
                "No realised track record for %s. Oracle grants no edge estimate, so size "
                "stays at zero until the ledger has trades." % proposal.strategy,
                0.0,
            ))
            return

        if not kelly.has_edge:
            self.emit(self._vote(
                proposal,
                Stance.REJECT,
                "Realised record for %s shows no edge: %.0f%% win rate at %.2f payoff. "
                "Kelly is zero." % (proposal.strategy, record.win_rate * 100.0, kelly.payoff_ratio),
                0.7,
            ))
            return

        if proposal.reward_risk < self.config.risk.min_reward_risk:
            self.emit(self._vote(
                proposal,
                Stance.REJECT,
                "Reward/risk of %.2f is below the %.2f floor." %
                (proposal.reward_risk, self.config.risk.min_reward_risk),
                0.6,
            ))
            return

        self.emit(self._vote(
            proposal,
            Stance.APPROVE,
            "Kelly %.4f applied from a %d-trade record. %s" %
            (kelly.applied, record.sample_size, kelly.note),
            0.6,
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


__all__ = ["Oracle", "StrategyRecord"]
