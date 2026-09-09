"""End-to-end loop tests.

Drives the full cycle through a crafted feed: scan, research, debate, size,
veto, execute, journal. The feed is deterministic, so these tests assert on
desk behaviour rather than on market luck.

Two behaviours matter equally here:
  - a good idea with a real track record can reach a fill
  - the desk stays in cash whenever any gate is unhappy
"""

from __future__ import annotations

import math
import os
import shutil
import sys
import tempfile
import time
import unittest
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cyrus.agents.athena import CalendarEvent
from cyrus.bus.messages import Decision, Observation, Proposal, Regime, Side, Verdict, Vote
from cyrus.config import load_desk_config
from cyrus.data.sources import BarSource, FeedInfo, SourceRegistry
from cyrus.desk import build_desk
from cyrus.indicators.core import Bar


class ScriptedSource(BarSource):
    """A feed that returns exactly the series each test wants."""

    info = FeedInfo(name="scripted", provider="test", realtime=False, note="TEST DATA")

    def __init__(self) -> None:
        self.series: Dict[str, List[Bar]] = {}

    def set(self, instrument: str, bars: List[Bar]) -> None:
        self.series[instrument] = bars

    def fetch(self, instrument: str, timeframe: str, limit: int = 300) -> List[Bar]:
        return list(self.series.get(instrument, []))[-limit:]


def flat_series(price: float, count: int, volume: float = 5_000_000.0) -> List[Bar]:
    """A quiet series with mild noise, so nothing triggers on its own."""
    bars: List[Bar] = []
    now = time.time() - count * 900
    for i in range(count):
        wobble = math.sin(i / 7.0) * price * 0.001
        close = price + wobble
        bars.append(
            Bar(
                ts=now + i * 900,
                open=close - price * 0.0005,
                high=close + price * 0.002,
                low=close - price * 0.002,
                close=close,
                volume=volume,
            )
        )
    return bars


def stretched_down_series(price: float, count: int = 120) -> List[Bar]:
    """Flat, then a sharp drop: a mean-reversion long setup."""
    bars = flat_series(price, count)
    drop = price * 0.03
    for i in range(count - 4, count):
        step = drop * (i - (count - 5)) / 4.0
        close = price - step
        bars[i] = Bar(
            ts=bars[i].ts,
            open=bars[i].open,
            high=close + price * 0.001,
            low=close - price * 0.001,
            close=close,
            volume=6_000_000.0,
        )
    return bars


def calm_vix(count: int = 260) -> List[Bar]:
    """A VIX series that reads risk-on rather than stress."""
    return flat_series(14.0, count, volume=0.0)


def uptrend_spy(count: int = 260) -> List[Bar]:
    """SPY with the 50 EMA above the 200 EMA, so Atlas sees risk_on."""
    bars: List[Bar] = []
    now = time.time() - count * 86400
    price = 400.0
    for i in range(count):
        price *= 1.0 + 0.0012 + math.sin(i / 11.0) * 0.0004
        bars.append(
            Bar(ts=now + i * 86400, open=price * 0.999, high=price * 1.004,
                low=price * 0.996, close=price, volume=80_000_000.0)
        )
    return bars


class DeskLoopFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cyrus_test_")
        self.feed = ScriptedSource()
        registry = SourceRegistry()
        registry.register("scripted", self.feed, preferred=True)

        self.config = load_desk_config()
        # Only the index book, to keep the assertions about one thing.
        for name, book in self.config.books.items():
            book.enabled = name == "us_index"
        self.config.books["us_index"].instruments = ["SPY"]

        self.feed.set("^VIX", calm_vix())
        self.feed.set("DX-Y.NYB", flat_series(104.0, 260, volume=0.0))

        self.desk = build_desk(
            config=self.config,
            ledger_dir=self.tmp,
            sources=registry,
            allow_network=False,
        )
        # Freeze the kernel clock inside the US cash session.
        self.desk.kernel.clock = (
            time.mktime((2026, 9, 9, 15, 0, 0, 2, 252, 0)) - time.timezone
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def give_oracle_a_record(self, strategy: str = "mean_reversion_z", trades: int = 400) -> None:
        """A realised, out-of-sample record so Oracle grants an edge."""
        record_holder = self.desk.oracle
        for i in range(trades):
            record_holder.record_outcome(strategy, 2.0 if i % 5 < 3 else -1.0)
        record_holder.records[strategy].out_of_sample = True


class TestQuietMarket(DeskLoopFixture):
    def test_no_setup_means_no_proposal_and_no_trade(self):
        """Micro-noise must not generate proposals.

        A z-score is scale-free, so a dead-quiet series throws large readings
        on noise alone. The min_edge_atr filter is what stops the desk from
        fading movement too small to pay for the spread.
        """
        self.feed.set("SPY", flat_series(500.0, 200))
        self.give_oracle_a_record()

        result = self.desk.run_cycle("us_index")
        self.assertEqual(len(result.proposals), 0, "noise must not raise a proposal")
        self.assertEqual(result.executed, 0)
        self.assertEqual(self.desk.state.open_count(), 0)
        self.assertTrue(
            any(d.action == "stand_down" for d in result.decisions),
            "a quiet market must produce an explicit stand-down",
        )

    def test_a_constant_series_produces_nothing(self):
        """Zero variance means the z-score is undefined, not infinite."""
        flat = [
            Bar(ts=time.time() - (200 - i) * 900, open=500.0, high=500.0,
                low=500.0, close=500.0, volume=5_000_000.0)
            for i in range(200)
        ]
        self.feed.set("SPY", flat)
        result = self.desk.run_cycle("us_index")
        self.assertEqual(len(result.proposals), 0)
        self.assertEqual(self.desk.bus.failures, [], "a flat series must not raise")


class TestFullApprovalPath(DeskLoopFixture):
    def test_a_repeatable_idea_with_a_record_reaches_a_fill(self):
        self.feed.set("SPY", stretched_down_series(500.0))
        self.give_oracle_a_record()

        result = self.desk.run_cycle("us_index")

        self.assertGreater(len(result.proposals), 0, "the setup should raise a proposal")
        self.assertGreater(len(result.survivors), 0, "it should survive the consensus runs")
        self.assertEqual(result.executed, 1, [v.reasons for v in result.verdicts])
        self.assertEqual(self.desk.state.open_count(), 1)

        position = self.desk.state.position("SPY")
        self.assertIsNotNone(position)
        self.assertEqual(position.side, Side.LONG)
        self.assertLess(position.stop, position.entry, "a long must have its stop below entry")

        # Risk actually taken never exceeds the per-trade cap.
        cap = self.config.equity * self.config.risk.max_risk_per_trade_pct / 100.0
        self.assertLessEqual(position.risk_usd, cap + 1e-6)

    def test_the_full_chain_is_reconstructable_from_the_bus(self):
        self.feed.set("SPY", stretched_down_series(500.0))
        self.give_oracle_a_record()
        result = self.desk.run_cycle("us_index")

        proposal = result.survivors[0]
        chain = self.desk.bus.correlated(proposal.correlation_id)
        kinds = {m.kind for m in chain}
        for expected in ("Proposal", "Vote", "Verdict", "OrderIntent", "Fill", "Decision"):
            self.assertIn(expected, kinds, "chain is missing %s" % expected)

    def test_cyrus_takes_accountability_on_every_decision(self):
        self.feed.set("SPY", stretched_down_series(500.0))
        self.give_oracle_a_record()
        result = self.desk.run_cycle("us_index")
        for decision in result.decisions:
            self.assertEqual(decision.accountable, "cyrus")


class TestSpecialistVetoes(DeskLoopFixture):
    def test_no_track_record_means_no_trade(self):
        """Oracle abstains without history, so quorum is never reached."""
        self.feed.set("SPY", stretched_down_series(500.0))
        result = self.desk.run_cycle("us_index")

        self.assertGreater(len(result.proposals), 0)
        self.assertEqual(result.executed, 0)
        self.assertEqual(self.desk.state.open_count(), 0)

    def test_a_scheduled_event_blocks_the_entry(self):
        self.feed.set("SPY", stretched_down_series(500.0))
        self.give_oracle_a_record()
        self.desk.athena.news_available = True
        self.desk.athena.add_event(
            CalendarEvent(instrument="SPY", label="CPI print", ts=time.time() + 6 * 3600)
        )

        result = self.desk.run_cycle("us_index")
        self.assertEqual(result.executed, 0)
        self.assertEqual(self.desk.state.open_count(), 0)

    def test_an_illiquid_name_never_gets_sized(self):
        thin = stretched_down_series(500.0)
        thin = [
            Bar(b.ts, b.open, b.high, b.low, b.close, volume=10.0) for b in thin
        ]
        self.feed.set("SPY", thin)
        self.give_oracle_a_record()

        result = self.desk.run_cycle("us_index")
        self.assertEqual(result.executed, 0)
        self.assertEqual(self.desk.state.open_count(), 0)


class TestKillSwitchInLoop(DeskLoopFixture):
    def test_drawdown_flattens_and_stops_the_cycle(self):
        self.feed.set("SPY", stretched_down_series(500.0))
        self.give_oracle_a_record()

        # Open a position on a healthy cycle.
        self.desk.run_cycle("us_index")
        self.assertEqual(self.desk.state.open_count(), 1)

        # Now blow through the flatten limit.
        self.desk.state.peak_equity = 100_000.0
        self.desk.state.equity = 88_000.0

        result = self.desk.run_cycle("us_index")
        self.assertIn("drawdown_flatten", result.tripped_switches)
        self.assertEqual(self.desk.state.open_count(), 0, "flatten must close everything")
        self.assertTrue(self.desk.state.halted)

        # And it stays shut on the next cycle.
        follow_up = self.desk.run_cycle("us_index")
        self.assertEqual(follow_up.executed, 0)

    def test_a_halted_desk_opens_nothing(self):
        self.feed.set("SPY", stretched_down_series(500.0))
        self.give_oracle_a_record()
        self.desk.state.halt("manual_review")

        result = self.desk.run_cycle("us_index")
        self.assertEqual(result.executed, 0)
        self.assertEqual(self.desk.state.open_count(), 0)


class TestJournalling(DeskLoopFixture):
    def test_every_decision_lands_on_disk(self):
        self.feed.set("SPY", stretched_down_series(500.0))
        self.give_oracle_a_record()
        self.desk.run_cycle("us_index")

        files = os.listdir(self.tmp)
        self.assertTrue(any(f.startswith("journal-") for f in files), files)
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "bus")))

        journal = [f for f in files if f.startswith("journal-")][0]
        with open(os.path.join(self.tmp, journal), "r", encoding="utf-8") as handle:
            content = handle.read()
        for entry_type in ("proposal", "verdict", "fill", "decision"):
            self.assertIn('"entry_type":"%s"' % entry_type, content)

    def test_rejections_are_journaled_too(self):
        """The refusals are the training data, so they must be recorded."""
        self.feed.set("SPY", stretched_down_series(500.0))
        self.desk.run_cycle("us_index")  # no oracle record: nothing executes
        self.assertGreater(len(self.desk.ledger.proposals), 0)

    def test_no_seat_raised_an_exception_on_the_bus(self):
        self.feed.set("SPY", stretched_down_series(500.0))
        self.give_oracle_a_record()
        self.desk.run_cycle("us_index")
        self.assertEqual(self.desk.bus.failures, [], self.desk.bus.failures)


class TestHumanInterface(DeskLoopFixture):
    def test_answer_reports_paper_mode_plainly(self):
        self.feed.set("SPY", flat_series(500.0, 200))
        self.desk.run_cycle("us_index")
        answer = self.desk.answer("where do we stand")
        self.assertIn("PAPER", answer)
        self.assertIn("paper", answer.lower())

    def test_answer_reports_the_halt_reason(self):
        self.desk.state.halt("drawdown_flatten")
        answer = self.desk.answer("are we trading")
        self.assertIn("drawdown_flatten", answer)


if __name__ == "__main__":
    unittest.main(verbosity=2)
