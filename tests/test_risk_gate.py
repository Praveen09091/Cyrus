"""The gate tests.

These are the tests that matter. Everything else in the repo can be rebuilt;
if these ever fail, the desk can lose money in a way it was designed not to.

Each test drives the deterministic path with crafted inputs, so none of them
depend on a data feed, a network, or a model.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cyrus.agents.base import Authority, SeatContext
from cyrus.agents.pilot import Pilot
from cyrus.agents.sentinel import Sentinel
from cyrus.bus.bus import Bus
from cyrus.bus.messages import Proposal, Ruling, Side, Verdict
from cyrus.config import load_desk_config
from cyrus.execution.paper import PaperBroker
from cyrus.risk import kelly
from cyrus.risk.kernel import RiskKernel
from cyrus.risk.sizing import size_proposal
from cyrus.risk.state import PortfolioState, Position


def make_proposal(**overrides) -> Proposal:
    """A structurally valid long with a 3:1 reward/risk."""
    defaults = dict(
        sender="chartist",
        book="us_index",
        instrument="SPY",
        side=Side.LONG,
        strategy="mean_reversion_z",
        entry=500.0,
        stop=495.0,
        target=515.0,
        timeframe="15m",
        thesis="Stretched below the mean.",
        bear_case="The stretch is the first leg of a trend.",
        invalidation="Close below 495.",
    )
    defaults.update(overrides)
    return Proposal(**defaults)


def good_kelly():
    """An edge estimate strong enough that Kelly is never the binding cap."""
    return kelly.estimate(
        win_rate=0.55,
        win_payoff=2.0,
        loss_payoff=1.0,
        sample_size=400,
        out_of_sample=True,
        desk_fraction=0.25,
    )


class GateFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_desk_config()
        self.state = PortfolioState(equity=self.config.equity)
        # Freeze the clock inside the US cash session so session checks are
        # deterministic rather than dependent on when the suite runs.
        wednesday_1500_utc = 1757430000.0  # 2025-09-09 15:00 UTC, a Tuesday
        self.kernel = RiskKernel(
            config=self.config, state=self.state, clock=self._session_clock()
        )

    def _session_clock(self) -> float:
        """An epoch inside 13:30-20:00 UTC on a weekday."""
        # 2026-09-09 was a Wednesday; 15:00 UTC is mid US session.
        return time.mktime((2026, 9, 9, 15, 0, 0, 2, 252, 0)) - time.timezone

    def review(self, proposal: Proposal, votes: int = 3, **kwargs) -> Verdict:
        return self.kernel.review(
            proposal, kelly=good_kelly(), approving_votes=votes, **kwargs
        )

    def assertRejectedFor(self, verdict: Verdict, needle: str) -> None:
        self.assertEqual(verdict.ruling, Ruling.REJECTED)
        self.assertIsNone(verdict.authorization_id)
        joined = " ".join(verdict.reasons)
        self.assertIn(needle, joined, "expected %r in %r" % (needle, joined))


class TestApprovalPath(GateFixture):
    def test_clean_proposal_is_approved_and_stamped(self):
        verdict = self.review(make_proposal())
        self.assertEqual(verdict.ruling, Ruling.APPROVED, verdict.reasons)
        self.assertTrue(verdict.authorization_id)
        self.assertGreater(verdict.quantity, 0)

    def test_approved_risk_never_exceeds_the_per_trade_cap(self):
        verdict = self.review(make_proposal())
        cap = self.config.equity * self.config.risk.max_risk_per_trade_pct / 100.0
        self.assertLessEqual(verdict.risk_usd, cap + 1e-6)


class TestStructuralFaults(GateFixture):
    def test_missing_stop_is_rejected(self):
        self.assertRejectedFor(self.review(make_proposal(stop=0.0)), "malformed:no_stop")

    def test_long_with_stop_above_entry_is_rejected(self):
        verdict = self.review(make_proposal(stop=505.0))
        self.assertRejectedFor(verdict, "malformed:long_stop_above_entry")

    def test_missing_bear_case_is_rejected(self):
        self.assertRejectedFor(self.review(make_proposal(bear_case="")), "no_bear_case")

    def test_no_direction_is_rejected(self):
        self.assertRejectedFor(self.review(make_proposal(side=Side.FLAT)), "no_direction")


class TestConsensus(GateFixture):
    def test_below_quorum_is_rejected(self):
        verdict = self.review(make_proposal(), votes=self.config.risk.consensus_quorum - 1)
        self.assertRejectedFor(verdict, "no_quorum")

    def test_zero_votes_is_rejected(self):
        self.assertRejectedFor(self.review(make_proposal(), votes=0), "no_quorum")


class TestRiskLimits(GateFixture):
    def test_thin_reward_risk_is_rejected(self):
        # 1:1 payoff against a 2.0 floor.
        verdict = self.review(make_proposal(target=505.0))
        self.assertRejectedFor(verdict, "reward_risk")

    def test_drawdown_flatten_blocks_new_risk(self):
        self.state.peak_equity = 100_000.0
        self.state.equity = 90_000.0  # 10% drawdown against a 9% limit
        self.assertRejectedFor(self.review(make_proposal()), "drawdown_flatten")

    def test_daily_loss_halt_blocks_new_risk(self):
        self.state.day_start_equity = 100_000.0
        self.state.equity = 96_000.0  # 4% down against a 3% limit
        self.assertRejectedFor(self.review(make_proposal()), "daily_loss_halt")

    def test_halted_desk_rejects_everything(self):
        self.state.halt("manual_review")
        self.assertRejectedFor(self.review(make_proposal()), "desk_halted")

    def test_position_slot_limit_is_enforced(self):
        for i in range(self.config.risk.max_open_positions):
            self.state.open_position(
                Position(
                    instrument="FILL%d" % i,
                    book="us_index",
                    side=Side.LONG,
                    quantity=1.0,
                    entry=100.0,
                    stop=99.0,
                )
            )
        self.assertRejectedFor(self.review(make_proposal()), "max_positions")

    def test_no_stacking_the_same_name(self):
        self.state.open_position(
            Position(
                instrument="SPY", book="us_index", side=Side.LONG,
                quantity=10.0, entry=498.0, stop=495.0,
            )
        )
        self.assertRejectedFor(self.review(make_proposal()), "already_long_or_short")

    def test_no_flipping_an_open_position(self):
        self.state.open_position(
            Position(
                instrument="SPY", book="us_index", side=Side.SHORT,
                quantity=10.0, entry=498.0, stop=502.0,
            )
        )
        self.assertRejectedFor(self.review(make_proposal()), "would_flip_position")


class TestSessionAndLiquidity(GateFixture):
    def test_closed_session_is_rejected(self):
        # Saturday 15:00 UTC.
        self.kernel.clock = time.mktime((2026, 9, 12, 15, 0, 0, 5, 255, 0)) - time.timezone
        self.assertRejectedFor(self.review(make_proposal()), "session_closed")

    def test_crypto_trades_outside_equity_hours(self):
        self.kernel.clock = time.mktime((2026, 9, 12, 3, 0, 0, 5, 255, 0)) - time.timezone
        verdict = self.review(
            make_proposal(book="crypto", instrument="BTC-USD", strategy="momentum_breakout",
                          entry=100_000.0, stop=97_000.0, target=112_000.0)
        )
        self.assertEqual(verdict.ruling, Ruling.APPROVED, verdict.reasons)

    def test_illiquid_name_is_rejected(self):
        verdict = self.review(make_proposal(), liquidity_dollar_volume=100_000.0)
        self.assertRejectedFor(verdict, "illiquid")

    def test_memecoin_requires_a_human(self):
        verdict = self.review(
            make_proposal(book="crypto", instrument="PEPE-USD", strategy="momentum_breakout",
                          entry=1.0, stop=0.9, target=1.4)
        )
        self.assertRejectedFor(verdict, "memecoin_requires_human")


class TestFactorBudget(GateFixture):
    def test_correlated_longs_share_one_budget(self):
        """SPY, QQQ, and BTC longs are one risk-on bet, not three.

        The consuming leg sits in a different book of the same factor, so the
        us_index book cap is untouched and the factor budget is the only thing
        that can bind. That is the whole point of the factor layer: a crypto
        long must be able to starve an index long.
        """
        factor = self.config.factor_for_book("us_index")
        self.assertIsNotNone(factor)
        self.assertIn("crypto", factor.books)
        budget = self.config.equity * factor.budget_pct / 100.0

        # One BTC long that eats the entire risk-on budget.
        self.state.open_position(
            Position(
                instrument="BTC-USD", book="crypto", side=Side.LONG,
                quantity=budget / 1000.0, entry=100_000.0, stop=99_000.0,
            )
        )
        self.assertAlmostEqual(
            self.state.factor_risk_usd(factor.books, direction=1), budget, places=6
        )

        sizing = size_proposal(make_proposal(), self.config, self.state, good_kelly())
        self.assertFalse(sizing.is_tradeable)
        self.assertIn("factor", sizing.binding_constraint)

    def test_opposite_direction_does_not_consume_the_budget(self):
        self.state.open_position(
            Position(
                instrument="BTC-USD", book="crypto", side=Side.SHORT,
                quantity=10.0, entry=100_000.0, stop=101_000.0,
            )
        )
        sizing = size_proposal(make_proposal(), self.config, self.state, good_kelly())
        self.assertTrue(sizing.is_tradeable)

    def test_tighter_book_cap_wins_over_a_looser_factor_budget(self):
        """Caps compose by taking the minimum; the binding one is always named."""
        self.state.open_position(
            Position(
                instrument="QQQ", book="us_index", side=Side.LONG,
                quantity=150.0, entry=450.0, stop=445.0,
            )
        )
        sizing = size_proposal(make_proposal(), self.config, self.state, good_kelly())
        self.assertEqual(sizing.binding_constraint, "book_remaining")


class TestKelly(unittest.TestCase):
    def test_no_edge_means_no_size(self):
        est = kelly.estimate(win_rate=0.4, win_payoff=1.0, sample_size=500, out_of_sample=True)
        self.assertEqual(est.applied, 0.0)
        self.assertFalse(est.has_edge)

    def test_no_history_means_no_size(self):
        est = kelly.estimate(win_rate=0.9, win_payoff=3.0, sample_size=0)
        self.assertEqual(est.applied, 0.0)

    def test_applied_is_always_below_raw_kelly(self):
        est = kelly.estimate(
            win_rate=0.6, win_payoff=2.0, sample_size=1000,
            out_of_sample=True, desk_fraction=0.25,
        )
        self.assertGreater(est.raw, 0.0)
        self.assertLess(est.applied, est.raw)
        self.assertLessEqual(est.applied, est.raw * 0.5)

    def test_in_sample_is_halved_against_out_of_sample(self):
        args = dict(win_rate=0.6, win_payoff=2.0, sample_size=500)
        oos = kelly.estimate(out_of_sample=True, **args)
        ins = kelly.estimate(out_of_sample=False, **args)
        self.assertAlmostEqual(ins.applied, oos.applied * 0.5, places=9)

    def test_thin_samples_are_discounted_hard(self):
        thin = kelly.estimate(win_rate=0.6, win_payoff=2.0, sample_size=10, out_of_sample=True)
        thick = kelly.estimate(win_rate=0.6, win_payoff=2.0, sample_size=1000, out_of_sample=True)
        self.assertLess(thin.applied, thick.applied)


class TestConfigGuards(unittest.TestCase):
    def test_full_kelly_is_rejected_by_config_validation(self):
        config = load_desk_config()
        config.risk.kelly_fraction = 1.0
        from cyrus.config import _validate

        with self.assertRaises(ValueError) as caught:
            _validate(config)
        self.assertIn("kelly_fraction", str(caught.exception))

    def test_shipped_config_is_paper(self):
        config = load_desk_config()
        self.assertEqual(config.mode, "paper")
        self.assertFalse(config.is_live)
        self.assertEqual(config.execution.venue, "paper")


class TestExecutionSeat(GateFixture):
    def setUp(self) -> None:
        super().setUp()
        self.bus = Bus()
        self.broker = PaperBroker(slippage_bps=5.0)
        self.sentinel = Sentinel(SeatContext(bus=self.bus), self.kernel)
        self.pilot = Pilot(SeatContext(bus=self.bus), self.broker, self.kernel)

    def test_pilot_refuses_an_unapproved_verdict(self):
        proposal = make_proposal()
        rejected = Verdict(proposal_id=proposal.message_id, ruling=Ruling.REJECTED)
        self.assertIsNone(self.pilot.execute(rejected, proposal))
        self.assertEqual(self.broker.log, [])

    def test_pilot_refuses_a_forged_authorization(self):
        proposal = make_proposal()
        forged = Verdict(
            proposal_id=proposal.message_id,
            ruling=Ruling.APPROVED,
            authorization_id="auth_deadbeefdeadbeef",
            quantity=10.0,
        )
        self.assertIsNone(self.pilot.execute(forged, proposal))
        self.assertEqual(self.broker.log, [])

    def test_authorization_is_single_use(self):
        proposal = make_proposal()
        verdict = self.sentinel.review(proposal, kelly=good_kelly(), approving_votes=3)
        self.assertTrue(verdict.approved, verdict.reasons)

        first = self.pilot.execute(verdict, proposal)
        self.assertIsNotNone(first)
        self.assertEqual(first.status, "filled")

        # Replaying the same stamp must not open a second position.
        second = self.pilot.execute(verdict, proposal)
        self.assertIsNone(second)

    def test_authorization_expires(self):
        proposal = make_proposal()
        verdict = self.sentinel.review(proposal, kelly=good_kelly(), approving_votes=3)
        auth = self.kernel.authorizations[verdict.authorization_id]
        auth.issued_ts = self.kernel._now() - (auth.ttl_seconds + 5.0)
        self.assertIsNone(self.pilot.execute(verdict, proposal))

    def test_authorization_cannot_be_reused_for_another_instrument(self):
        proposal = make_proposal()
        verdict = self.sentinel.review(proposal, kelly=good_kelly(), approving_votes=3)
        other = make_proposal(instrument="QQQ")
        other.message_id = proposal.message_id  # same id, different name
        self.assertIsNone(self.pilot.execute(verdict, other))

    def test_slippage_always_works_against_the_order(self):
        proposal = make_proposal()
        verdict = self.sentinel.review(proposal, kelly=good_kelly(), approving_votes=3)
        fill = self.pilot.execute(verdict, proposal)
        self.assertGreater(fill.price, proposal.entry)  # a long pays up

    def test_a_proposing_seat_cannot_forge_a_verdict(self):
        """Authority is structural: only the risk seat may emit a Verdict."""
        from cyrus.agents.base import Agent

        class RogueSeat(Agent):
            name = "rogue"
            authority = Authority.PROPOSE

        rogue = RogueSeat(SeatContext(bus=self.bus))
        with self.assertRaises(PermissionError):
            rogue.emit(Verdict(ruling=Ruling.APPROVED, authorization_id="auth_x"))

    def test_flatten_needs_no_authorization(self):
        """Reducing risk is always permitted; only adding risk needs a stamp."""
        proposal = make_proposal()
        verdict = self.sentinel.review(proposal, kelly=good_kelly(), approving_votes=3)
        self.pilot.execute(verdict, proposal)
        self.assertEqual(self.state.open_count(), 1)

        fills = self.pilot.flatten({"SPY": 490.0}, reason="drawdown_flatten")
        self.assertEqual(len(fills), 1)
        self.assertEqual(self.state.open_count(), 0)


class TestKillSwitches(GateFixture):
    def test_drawdown_trips_flatten_and_halts(self):
        self.state.peak_equity = 100_000.0
        self.state.equity = 90_000.0
        tripped = self.kernel.evaluate_kill_switches()
        self.assertIn("drawdown_flatten", tripped)
        self.assertTrue(self.state.halted)

    def test_daily_halt_clears_with_the_new_day(self):
        self.state.day_start_equity = 100_000.0
        self.state.equity = 96_000.0
        self.kernel.evaluate_kill_switches()
        self.assertTrue(self.state.halted)

        self.state.start_new_day()
        self.assertFalse(self.state.halted)

    def test_drawdown_halt_does_not_clear_with_the_new_day(self):
        """A flatten needs a human. It must not expire overnight."""
        self.state.peak_equity = 100_000.0
        self.state.equity = 90_000.0
        self.kernel.evaluate_kill_switches()
        self.state.start_new_day()
        self.assertTrue(self.state.halted)

    def test_burn_buffer_breach_blocks_trading(self):
        self.config.paper_equity = 100.0  # below 6 months of burn
        ok, reason = self.kernel.check_burn_coverage()
        self.assertFalse(ok)
        self.assertIn("burn_buffer", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
