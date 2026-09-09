"""Ledger: the book.

Append-only, by mandate. Ledger records every decision, verdict, order, and
fill, including the rejections. The rejections are the most valuable rows in
the file: they are the record of what the desk refused and why, which is what
turns a loss into a rule instead of a mood.

Ledger never rewrites history. Corrections are new rows.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from cyrus.agents.base import Agent, Authority, SeatClass, SeatContext
from cyrus.bus.messages import (
    Alert,
    Decision,
    Envelope,
    Evidence,
    Fill,
    LedgerEntry,
    Observation,
    Proposal,
    Ruling,
    Verdict,
)


@dataclass
class ClosedTrade:
    instrument: str
    book: str
    strategy: str
    entry: float
    exit: float
    quantity: float
    pnl: float
    thesis: str
    grade: str = ""
    lesson: str = ""


class Ledger(Agent):
    """Not a diary. The evidence base every other seat is judged against."""

    name = "ledger"
    seat_class = SeatClass.RECORD
    authority = Authority.RECORD
    llm = "optional"

    def __init__(self, ctx: SeatContext, directory: str) -> None:
        self.directory = directory
        self.proposals: Dict[str, Proposal] = {}
        self.verdicts: List[Verdict] = []
        self.closed: List[ClosedTrade] = []
        self.rejection_counts: Dict[str, int] = {}
        os.makedirs(self.directory, exist_ok=True)
        super().__init__(ctx)

    def register(self) -> None:
        self.bus.subscribe(Proposal, self._on_proposal)
        self.bus.subscribe(Verdict, self._on_verdict)
        self.bus.subscribe(Fill, self._on_fill)
        self.bus.subscribe(Decision, self._on_decision)
        self.bus.subscribe(Alert, self._on_alert)

    # --- append-only writing ---------------------------------------------

    def _path(self, name: str) -> str:
        return os.path.join(self.directory, name)

    def append(self, entry_type: str, payload: Dict[str, Any]) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "entry_type": entry_type,
            "payload": payload,
        }
        line = json.dumps(record, separators=(",", ":"), default=str)
        day = datetime.now(timezone.utc).strftime("%Y-%m")
        with open(self._path("journal-%s.jsonl" % day), "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    # --- subscriptions ----------------------------------------------------

    def _on_proposal(self, proposal: Proposal) -> None:
        self.proposals[proposal.message_id] = proposal
        self.append("proposal", proposal.to_dict())

    def _on_verdict(self, verdict: Verdict) -> None:
        self.verdicts.append(verdict)
        self.append("verdict", verdict.to_dict())
        if verdict.ruling == Ruling.REJECTED:
            for reason in verdict.reasons:
                key = reason.split(":")[0]
                self.rejection_counts[key] = self.rejection_counts.get(key, 0) + 1

    def _on_fill(self, fill: Fill) -> None:
        self.append("fill", fill.to_dict())

    def _on_decision(self, decision: Decision) -> None:
        self.append("decision", decision.to_dict())

    def _on_alert(self, alert: Alert) -> None:
        self.append("alert", alert.to_dict())

    # --- grading ----------------------------------------------------------

    def close_trade(
        self,
        instrument: str,
        book: str,
        strategy: str,
        entry: float,
        exit_price: float,
        quantity: float,
        pnl: float,
        thesis: str,
    ) -> ClosedTrade:
        """Record a closed trade and grade it against its original thesis."""
        grade, lesson = self._grade(pnl, entry, exit_price)
        trade = ClosedTrade(
            instrument=instrument,
            book=book,
            strategy=strategy,
            entry=entry,
            exit=exit_price,
            quantity=quantity,
            pnl=pnl,
            thesis=thesis,
            grade=grade,
            lesson=lesson,
        )
        self.closed.append(trade)
        self.append("closed_trade", {
            "instrument": instrument,
            "book": book,
            "strategy": strategy,
            "entry": entry,
            "exit": exit_price,
            "quantity": quantity,
            "pnl": pnl,
            "grade": grade,
            "lesson": lesson,
            "thesis": thesis,
        })
        return trade

    def _grade(self, pnl: float, entry: float, exit_price: float) -> tuple:
        """Grade the process, not only the outcome.

        A profitable trade taken for the wrong reason is still a bad trade, and
        a loss that respected its stop is a correctly executed one.
        """
        if pnl > 0:
            return "win", "Thesis played out. Confirm it was the thesis and not luck."
        if pnl == 0:
            return "scratch", "Flat. Check whether the setup ever actually triggered."
        move = abs(exit_price - entry) / entry * 100.0 if entry else 0.0
        return (
            "loss",
            "Stop honoured after a %.2f%% adverse move. Record which part of the thesis failed."
            % move,
        )

    # --- reporting --------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        wins = [t for t in self.closed if t.pnl > 0]
        losses = [t for t in self.closed if t.pnl < 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        return {
            "closed_trades": len(self.closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(self.closed)) if self.closed else 0.0,
            "gross_win": gross_win,
            "gross_loss": gross_loss,
            "net_pnl": gross_win - gross_loss,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else 0.0,
            "avg_win": (gross_win / len(wins)) if wins else 0.0,
            "avg_loss": (gross_loss / len(losses)) if losses else 0.0,
            "verdicts": len(self.verdicts),
            "rejections": sum(1 for v in self.verdicts if v.ruling == Ruling.REJECTED),
        }

    def candidate_rules(self, threshold: int = 3) -> List[Observation]:
        """Repeated rejections are a rule waiting to be written.

        When the same gate fires over and over, the fix is upstream: stop
        generating that proposal rather than rejecting it forever.
        """
        out: List[Observation] = []
        for reason, count in sorted(self.rejection_counts.items(), key=lambda kv: -kv[1]):
            if count < threshold:
                continue
            observation = Observation(
                sender=self.name,
                subject="process",
                statement=(
                    "Sentinel rejected %d proposals for '%s'. The generator upstream should "
                    "stop producing these rather than having them vetoed every cycle."
                    % (count, reason)
                ),
                evidence=Evidence.SOURCED,
                confidence=0.7,
                sources=["ledger"],
                data={"reason": reason, "count": count},
            )
            self.emit(observation)
            out.append(observation)
        return out


__all__ = ["Ledger", "ClosedTrade"]
