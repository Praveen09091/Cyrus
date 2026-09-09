"""Cyrus: the brain.

Cyrus routes work to the specialists, runs the desk more than once, keeps only
what survives quorum, decides, and owns the outcome. It is the only seat that
speaks to the human, and it never touches an order.

The consensus gate is the answer to a real weakness: a language model asked the
same question twice gives different answers. One run is a sample, not a signal.
So Cyrus runs the desk ``consensus_runs`` times and requires the same idea to
survive every run and to clear ``consensus_quorum`` approving seats. When the
desk disagrees with itself, the output is cash.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from cyrus.agents.athena import Athena
from cyrus.agents.atlas import Atlas
from cyrus.agents.base import Agent, Authority, SeatClass, SeatContext
from cyrus.agents.chartist import Chartist
from cyrus.agents.ledger import Ledger
from cyrus.agents.oracle import Oracle
from cyrus.agents.pilot import Pilot
from cyrus.agents.quartermaster import Quartermaster
from cyrus.agents.scout import Scout
from cyrus.agents.sentinel import Sentinel
from cyrus.bus.messages import (
    Decision,
    Observation,
    Proposal,
    Regime,
    ScanRequest,
    Stance,
    Verdict,
    Vote,
)
from cyrus.config import BookConfig, DeskConfig


@dataclass
class CycleResult:
    """What one full pass of the desk produced."""

    cycle_id: str
    book: str
    regime: Regime
    observations: int = 0
    proposals: List[Proposal] = field(default_factory=list)
    survivors: List[Proposal] = field(default_factory=list)
    verdicts: List[Verdict] = field(default_factory=list)
    decisions: List[Decision] = field(default_factory=list)
    tripped_switches: List[str] = field(default_factory=list)
    stood_down: List[str] = field(default_factory=list)

    @property
    def executed(self) -> int:
        return sum(1 for v in self.verdicts if v.approved)


class Cyrus(Agent):
    name = "cyrus"
    seat_class = SeatClass.ORCHESTRATOR
    authority = Authority.DECIDE
    llm = "required"

    def __init__(
        self,
        ctx: SeatContext,
        config: DeskConfig,
        atlas: Atlas,
        scout: Scout,
        athena: Athena,
        chartist: Chartist,
        oracle: Oracle,
        sentinel: Sentinel,
        pilot: Pilot,
        ledger: Ledger,
        quartermaster: Quartermaster,
    ) -> None:
        self.config = config
        self.atlas = atlas
        self.scout = scout
        self.athena = athena
        self.chartist = chartist
        self.oracle = oracle
        self.sentinel = sentinel
        self.pilot = pilot
        self.ledger = ledger
        self.quartermaster = quartermaster
        self.votes: Dict[str, List[Vote]] = {}
        super().__init__(ctx)

    def register(self) -> None:
        self.bus.subscribe(Vote, self._collect_vote)

    def _collect_vote(self, vote: Vote) -> None:
        self.votes.setdefault(vote.proposal_id, []).append(vote)

    # --- the loop ---------------------------------------------------------

    def run_cycle(self, book_name: str) -> CycleResult:
        """scan, research, debate, size, veto, execute, journal."""
        cycle_id = "cyc_" + uuid.uuid4().hex[:10]
        self.ctx.cycle_id = cycle_id
        for seat in (self.atlas, self.scout, self.athena, self.chartist, self.oracle,
                     self.sentinel, self.pilot, self.ledger, self.quartermaster):
            seat.ctx.cycle_id = cycle_id

        book = self.config.book(book_name)
        result = CycleResult(cycle_id=cycle_id, book=book_name, regime=Regime.UNKNOWN)

        if book is None or not book.enabled:
            result.stood_down.append("book_disabled")
            self._stand_down(book_name, "Book %s is not enabled." % book_name, result)
            return result

        # Risk sweep runs first. A tripped switch outranks any idea.
        result.tripped_switches = self.sentinel.sweep()
        if result.tripped_switches:
            self._handle_switches(result, book)
            return result

        # 1. Scan
        self.emit(
            ScanRequest(
                sender=self.name,
                cycle_id=cycle_id,
                book=book_name,
                instruments=book.instruments,
                timeframe=book.timeframe,
                reason="scheduled cycle",
            )
        )

        # 2. Research
        macro = self.atlas.read_regime()
        result.regime = macro.regime
        scout_obs = self.scout.scan(book.instruments, book.timeframe)
        athena_obs = self.athena.brief(book.instruments)
        result.observations = 1 + len(scout_obs) + len(athena_obs)

        # 3. Debate, repeated. Only ideas that survive every run continue.
        survivors, all_proposals = self._debate(book, macro.regime)
        result.proposals = all_proposals
        result.survivors = survivors

        if not survivors:
            reason = (
                "No idea survived %d desk runs in %s. Regime %s."
                % (self.config.risk.consensus_runs, book_name, macro.regime.value)
            )
            self._stand_down(book_name, reason, result)
            return result

        # 4-6. Size, veto, execute.
        for proposal in survivors:
            self.oracle.spectral_report(proposal.instrument, self.chartist.bars_for(proposal.instrument))
            self.oracle.size_request(proposal)
            kelly = self.oracle.kelly_for(proposal)
            votes = self.votes.get(proposal.message_id, [])
            approvals = sum(1 for v in votes if v.stance == Stance.APPROVE)

            verdict = self.sentinel.review(
                proposal,
                kelly=kelly,
                approving_votes=approvals,
                liquidity_dollar_volume=self.scout.dollar_volume_for(proposal.instrument),
            )
            result.verdicts.append(verdict)

            if verdict.approved:
                self.pilot.execute(verdict, proposal)
                decision = self._decide(proposal, votes, "trade", verdict)
            else:
                decision = self._decide(proposal, votes, "stand_down", verdict)
            result.decisions.append(decision)

        # 7. Journal
        self.ledger.candidate_rules()
        self.quartermaster.report()
        return result

    # --- debate -----------------------------------------------------------

    def _debate(self, book: BookConfig, regime: Regime) -> Tuple[List[Proposal], List[Proposal]]:
        """Run the analysis seats N times and keep only repeatable ideas.

        Survival is keyed on (instrument, side, strategy) rather than on the
        message id, since each run produces fresh envelopes. An idea that
        appears in one run and not the next is noise.
        """
        runs = max(1, self.config.risk.consensus_runs)
        quorum = self.config.risk.consensus_quorum

        seen: Dict[Tuple[str, str, str], List[Proposal]] = {}
        every: List[Proposal] = []

        for _ in range(runs):
            proposals = self.chartist.analyze_book(book, regime.value)
            every.extend(proposals)
            for proposal in proposals:
                key = (proposal.instrument, proposal.side.value, proposal.strategy)
                seen.setdefault(key, []).append(proposal)

        survivors: List[Proposal] = []
        for key, group in seen.items():
            if len(group) < runs:
                continue  # did not repeat across runs
            candidate = group[-1]
            votes = self.votes.get(candidate.message_id, [])
            approvals = sum(1 for v in votes if v.stance == Stance.APPROVE)
            rejections = sum(1 for v in votes if v.stance == Stance.REJECT)
            if rejections > 0:
                continue  # any specialist rejection kills the idea
            if approvals < quorum:
                continue
            survivors.append(candidate)

        return survivors, every

    # --- decisions --------------------------------------------------------

    def _decide(
        self,
        proposal: Proposal,
        votes: List[Vote],
        action: str,
        verdict: Verdict,
    ) -> Decision:
        dissent = [
            "%s: %s" % (v.sender, v.reason) for v in votes if v.stance != Stance.APPROVE
        ]
        if action == "trade":
            rationale = (
                "Took %s %s at %.4f, stop %.4f, target %.4f, size %.6f. %s Reward/risk %.2f. "
                "Accountability sits with Cyrus regardless of which seat sourced the idea."
                % (
                    proposal.side.value,
                    proposal.instrument,
                    proposal.entry,
                    proposal.stop,
                    proposal.target,
                    verdict.quantity,
                    "; ".join(verdict.reasons),
                    proposal.reward_risk,
                )
            )
        else:
            rationale = (
                "Stood down on %s %s. Sentinel rejected: %s. The refusal is the correct "
                "outcome, not a missed opportunity."
                % (proposal.side.value, proposal.instrument, "; ".join(verdict.reasons))
            )

        decision = Decision(
            sender=self.name,
            cycle_id=proposal.cycle_id,
            correlation_id=proposal.correlation_id,
            book=proposal.book,
            action=action,
            proposal_id=proposal.message_id,
            rationale=rationale,
            dissent=dissent,
            votes={v.sender: v.stance.value for v in votes},
            accountable="cyrus",
        )
        self.emit(decision)
        return decision

    def _stand_down(self, book_name: str, reason: str, result: CycleResult) -> None:
        decision = Decision(
            sender=self.name,
            cycle_id=self.ctx.cycle_id,
            book=book_name,
            action="stand_down",
            rationale=reason + " Cash is a position.",
            accountable="cyrus",
        )
        self.emit(decision)
        result.decisions.append(decision)

    def _handle_switches(self, result: CycleResult, book: BookConfig) -> None:
        """A kill switch tripped. Flatten if required, then stop the cycle."""
        if "drawdown_flatten" in result.tripped_switches:
            prices = {
                instrument: (self.chartist.context.get(instrument, {}) or {}).get("close", 0.0)
                for instrument in self.chartist.context
            }
            self.pilot.flatten(prices, reason="drawdown_flatten")
        self._stand_down(
            book.name,
            "Risk switches tripped: %s. No new risk this cycle."
            % ", ".join(result.tripped_switches),
            result,
        )

    # --- human interface --------------------------------------------------

    def answer(self, question: str) -> str:
        """Cyrus is the only voice. Answers come from state, not from vibes."""
        stats = self.ledger.stats()
        burn = self.quartermaster.report()
        state = self.sentinel.kernel.state
        lines = [
            "Question: %s" % question,
            "",
            "Desk state: mode=%s, equity=$%.2f, open positions=%d, halted=%s%s."
            % (
                self.config.mode,
                self.config.equity,
                state.open_count(),
                state.halted,
                (" (%s)" % state.halt_reason) if state.halted else "",
            ),
            "Regime: %s." % self.atlas.regime.value,
            "Drawdown %.2f%% against a %.2f%% flatten limit; today %.2f%% against a %.2f%% halt."
            % (
                state.drawdown_pct,
                self.config.risk.drawdown_flatten_pct,
                state.day_loss_pct,
                self.config.risk.daily_loss_halt_pct,
            ),
            "Record: %d closed trades, %.0f%% win rate, net $%.2f, profit factor %.2f."
            % (
                stats["closed_trades"],
                stats["win_rate"] * 100.0,
                stats["net_pnl"],
                stats["profit_factor"],
            ),
            "Verdicts: %d issued, %d rejected." % (stats["verdicts"], stats["rejections"]),
            "Treasury: %s" % burn.recommendation,
        ]
        if self.config.mode == "paper":
            lines.append(
                "All figures are PAPER. No real money has moved and none can until a human "
                "changes the mode and Sentinel's gates pass."
            )
        return "\n".join(lines)


__all__ = ["Cyrus", "CycleResult"]
