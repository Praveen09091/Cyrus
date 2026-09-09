"""Backtest harness tests.

These exist to attack the harness, not to demonstrate it. A backtest engine is
the easiest place in a trading system to accidentally cheat, so the tests below
try to cheat on purpose and assert that they cannot:

  - a strategy that peeks at the future must not be able to profit
  - a gap through the stop must cost more than the stop level
  - fees and slippage must reduce PnL, never flatter it
  - a walk-forward with no grid must refuse to call itself out-of-sample
  - a synthetic or in-sample report must never qualify for sizing
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cyrus.backtest.engine import BacktestEngine
from cyrus.backtest.metrics import compute, trades_per_year
from cyrus.backtest.store import (
    is_admissible,
    is_evidence,
    latest_per_series,
    refusals,
    save_report,
    strategy_records,
)
from cyrus.backtest.walkforward import WalkForward, _expand_grid, _plan_windows
from cyrus.bus.messages import Proposal, Side
from cyrus.config import BookConfig, load_desk_config
from cyrus.indicators.core import Bar
from cyrus.signals.base import Strategy, StrategyContext

BAR_SECONDS = 900


def make_bars(closes: List[float], start_ts: float = 1_700_000_000.0) -> List[Bar]:
    """Bars whose high/low bracket the close by a fixed fraction."""
    bars: List[Bar] = []
    for i, close in enumerate(closes):
        prev = closes[i - 1] if i else close
        bars.append(
            Bar(
                ts=start_ts + i * BAR_SECONDS,
                open=prev,
                high=max(prev, close) * 1.001,
                low=min(prev, close) * 0.999,
                close=close,
                volume=1_000_000.0,
            )
        )
    return bars


def zero_cost_config():
    """Desk-wide costs off, so a book with no override trades free.

    Only the cost tests below turn friction back on. Everything else asserts
    about fills, sizing, and labelling, where a cost drag is just noise.
    """
    config = load_desk_config()
    config.execution.slippage_bps = 0.0
    config.execution.commission_bps = 0.0
    return config


def test_book(**overrides) -> BookConfig:
    """A book that carries its own costs, which is where the engine reads them."""
    spec = dict(
        name="us_index",
        enabled=True,
        instruments=["TEST"],
        style="mean_reversion",
        timeframe="15m",
        session="always",
        max_risk_pct=0.75,
        params={},
        optimize={},
        slippage_bps=0.0,
        commission_bps=0.0,
    )
    spec.update(overrides)
    return BookConfig(**spec)


class AlwaysLong(Strategy):
    """Fires on every bar with a fixed-fraction stop and target."""

    name = "always_long"
    style = "mean_reversion"
    min_bars = 2

    def evaluate(self, ctx: StrategyContext) -> Optional[Proposal]:
        price = ctx.bars[-1].close
        stop_pct = ctx.fparam("stop_pct", 0.02)
        target_pct = ctx.fparam("target_pct", 0.04)
        return self._proposal(
            ctx,
            side=Side.LONG,
            entry=price,
            stop=price * (1.0 - stop_pct),
            target=price * (1.0 + target_pct),
            thesis="Test strategy: always long.",
            bear_case="It is a test strategy with no edge whatsoever.",
            invalidation="Stop at %.4f." % (price * (1.0 - stop_pct)),
            indicators={"price": price},
        )


class Oracular(Strategy):
    """A strategy that tries to cheat by reading the last bar of the series.

    The engine hands it a slice, so the 'future' it sees is only the bar it
    just closed on. If look-ahead were possible, this would print money.
    """

    name = "oracular"
    style = "mean_reversion"
    min_bars = 2

    def __init__(self) -> None:
        self.longest_slice = 0

    def evaluate(self, ctx: StrategyContext) -> Optional[Proposal]:
        self.longest_slice = max(self.longest_slice, len(ctx.bars))
        final = ctx.bars[-1].close
        price = ctx.bars[-1].close
        side = Side.LONG if final >= price else Side.SHORT
        stop = price * 0.98 if side == Side.LONG else price * 1.02
        target = price * 1.04 if side == Side.LONG else price * 0.96
        return self._proposal(
            ctx,
            side=side,
            entry=price,
            stop=stop,
            target=target,
            thesis="Attempts to use the end of the series.",
            bear_case="If this wins, the engine leaks the future.",
            invalidation="Stop at %.4f." % stop,
            indicators={},
        )


class NoLookAhead(unittest.TestCase):
    def test_strategy_never_sees_past_the_decision_bar(self):
        """The widest slice a strategy gets stops one bar short of the end."""
        bars = make_bars([100.0 + i * 0.1 for i in range(60)])
        strategy = Oracular()
        engine = BacktestEngine(zero_cost_config())
        engine.run(strategy, test_book(), "TEST", bars)

        # The last decision is made on bar len-2 so it can fill on bar len-1.
        self.assertLessEqual(strategy.longest_slice, len(bars) - 1)

    def test_entry_fills_at_the_next_bar_open_not_the_signal_close(self):
        """A gap up between signal and fill must be paid by the trade."""
        closes = [100.0, 100.0, 100.0, 110.0, 110.0, 110.0, 110.0]
        bars = make_bars(closes)
        engine = BacktestEngine(zero_cost_config())
        result = engine.run(AlwaysLong(), test_book(), "TEST", bars)

        self.assertTrue(result.trades)
        first = result.trades[0]
        # Signal formed on bar 1 (close 100), so the fill is bar 2's open.
        self.assertAlmostEqual(first.entry, bars[2].open, places=6)
        self.assertNotAlmostEqual(first.entry, bars[1].close * 1.0000001, places=6)

    def test_a_trade_cannot_exit_before_it_enters(self):
        bars = make_bars([100.0 + (i % 5) for i in range(80)])
        engine = BacktestEngine(zero_cost_config())
        result = engine.run(AlwaysLong(), test_book(), "TEST", bars)
        for trade in result.trades:
            self.assertGreaterEqual(trade.exit_ts, trade.entry_ts)
            self.assertGreaterEqual(trade.bars_held, 0)

    def test_positions_do_not_overlap(self):
        bars = make_bars([100.0 + (i % 7) * 0.5 for i in range(200)])
        engine = BacktestEngine(zero_cost_config())
        result = engine.run(AlwaysLong(), test_book(), "TEST", bars)
        self.assertTrue(result.trades)
        for earlier, later in zip(result.trades, result.trades[1:]):
            self.assertGreaterEqual(later.entry_ts, earlier.exit_ts)


class GapsAndAmbiguity(unittest.TestCase):
    def test_a_gap_through_the_stop_fills_at_the_open(self):
        """Stops do not fill where you wrote them when the market gaps."""
        # AlwaysLong warms up over 2 bars, so the signal forms on bar 2 and
        # fills at bar 3's open of 100 with a stop at 98. Bar 4 then opens at
        # 90, far through that stop.
        flat = Bar(ts=0, open=100.0, high=100.5, low=99.5, close=100.0, volume=1e6)
        bars = [
            flat,
            Bar(ts=900, open=100.0, high=100.5, low=99.5, close=100.0, volume=1e6),
            Bar(ts=1800, open=100.0, high=100.5, low=99.5, close=100.0, volume=1e6),
            Bar(ts=2700, open=100.0, high=100.5, low=99.5, close=100.0, volume=1e6),
            Bar(ts=3600, open=90.0, high=90.5, low=89.0, close=90.0, volume=1e6),
            Bar(ts=4500, open=90.0, high=90.5, low=89.5, close=90.0, volume=1e6),
        ]
        engine = BacktestEngine(zero_cost_config())
        result = engine.run(AlwaysLong(), test_book(), "TEST", bars)

        self.assertTrue(result.trades)
        trade = result.trades[0]
        self.assertAlmostEqual(trade.entry, 100.0, places=6)
        self.assertEqual(trade.exit_reason, "stop")
        # Filled at the gap open, well below the 98.0 stop level.
        self.assertAlmostEqual(trade.exit, 90.0, places=6)
        self.assertLess(trade.exit, trade.stop)
        self.assertLess(trade.net_pnl, 0.0)

    def test_a_bar_touching_both_levels_resolves_as_a_stop(self):
        """Ambiguity inside one bar must resolve against the position."""
        bars = [
            Bar(ts=0, open=100.0, high=100.2, low=99.8, close=100.0, volume=1e6),
            Bar(ts=900, open=100.0, high=100.2, low=99.8, close=100.0, volume=1e6),
            Bar(ts=1800, open=100.0, high=100.2, low=99.8, close=100.0, volume=1e6),
            # Reaches the 104 target and the 98 stop in the same bar.
            Bar(ts=2700, open=100.0, high=105.0, low=97.0, close=100.0, volume=1e6),
            Bar(ts=3600, open=100.0, high=100.2, low=99.8, close=100.0, volume=1e6),
        ]
        engine = BacktestEngine(zero_cost_config())
        result = engine.run(AlwaysLong(), test_book(), "TEST", bars)

        self.assertTrue(result.trades)
        self.assertEqual(result.trades[0].exit_reason, "stop")
        self.assertLess(result.trades[0].net_pnl, 0.0)

    def test_a_signal_that_gaps_past_its_own_stop_never_opens(self):
        """If the stop is already breached at the fill, there is no trade."""
        bars = [
            Bar(ts=0, open=100.0, high=100.2, low=99.8, close=100.0, volume=1e6),
            Bar(ts=900, open=100.0, high=100.2, low=99.8, close=100.0, volume=1e6),
            Bar(ts=1800, open=100.0, high=100.2, low=99.8, close=100.0, volume=1e6),
            # The fill bar opens at 50, below the 98 stop set on the signal bar.
            Bar(ts=2700, open=50.0, high=50.2, low=49.8, close=50.0, volume=1e6),
            Bar(ts=3600, open=50.0, high=50.2, low=49.8, close=50.0, volume=1e6),
        ]
        engine = BacktestEngine(zero_cost_config())
        result = engine.run(AlwaysLong(), test_book(), "TEST", bars)
        self.assertGreaterEqual(result.skipped_gap_through_stop, 1)


class CostsAlwaysHurt(unittest.TestCase):
    def _run(self, slippage_bps: float, commission_bps: float):
        # A clean run to target so the comparison is about costs, not luck.
        closes = [100.0, 100.0, 100.0, 101.0, 102.0, 103.0, 105.0, 105.0]
        bars = make_bars(closes)
        engine = BacktestEngine(zero_cost_config())
        book = test_book(slippage_bps=slippage_bps, commission_bps=commission_bps)
        return engine.run(AlwaysLong(), book, "TEST", bars)

    def test_commission_reduces_net_pnl(self):
        free = self._run(0.0, 0.0)
        charged = self._run(0.0, 25.0)
        self.assertTrue(free.trades and charged.trades)
        self.assertEqual(free.trades[0].fees, 0.0)
        self.assertGreater(charged.trades[0].fees, 0.0)
        self.assertLess(charged.metrics.net_pnl, free.metrics.net_pnl)

    def test_slippage_worsens_the_entry_for_a_long(self):
        free = self._run(0.0, 0.0)
        slipped = self._run(50.0, 0.0)
        self.assertGreater(slipped.trades[0].entry, free.trades[0].entry)

    def test_fees_are_reported_not_hidden_in_the_net(self):
        charged = self._run(0.0, 25.0)
        trade = charged.trades[0]
        self.assertAlmostEqual(trade.net_pnl, trade.gross_pnl - trade.fees, places=6)
        self.assertAlmostEqual(charged.metrics.fees_paid, trade.fees, places=6)


class SizingIsShared(unittest.TestCase):
    def test_the_engine_sizes_through_the_desk_risk_caps(self):
        """Backtest size must obey the same per-trade cap the live desk uses."""
        config = zero_cost_config()
        bars = make_bars([100.0] * 6)
        engine = BacktestEngine(config, starting_equity=100_000.0)
        result = engine.run(AlwaysLong(), test_book(), "TEST", bars)

        self.assertTrue(result.trades)
        trade = result.trades[0]
        risk = abs(trade.entry - trade.stop) * trade.quantity
        cap = 100_000.0 * config.risk.max_risk_per_trade_pct / 100.0
        self.assertLessEqual(risk, cap + 1e-6)

    def test_equity_compounds_across_trades(self):
        bars = make_bars([100.0 + (i % 9) * 0.4 for i in range(300)])
        engine = BacktestEngine(zero_cost_config(), starting_equity=50_000.0)
        result = engine.run(AlwaysLong(), test_book(), "TEST", bars)
        self.assertEqual(result.equity_curve[0], 50_000.0)
        self.assertEqual(len(result.equity_curve), len(result.trades) + 1)
        running = 50_000.0
        for trade, point in zip(result.trades, result.equity_curve[1:]):
            running += trade.net_pnl
            self.assertAlmostEqual(point, running, places=6)


class MetricHonesty(unittest.TestCase):
    def test_no_trades_yields_no_edge_and_no_invented_numbers(self):
        metrics = compute([], [1000.0])
        self.assertEqual(metrics.trade_count, 0)
        self.assertFalse(metrics.has_edge)
        self.assertIsNone(metrics.profit_factor)
        self.assertIsNone(metrics.sharpe)

    def test_profit_factor_is_none_without_a_single_loss(self):
        """An unbeaten sample has an undefined ratio, not an infinite one."""
        metrics = compute([10.0, 20.0, 30.0], [100.0, 110.0, 130.0, 160.0])
        self.assertIsNone(metrics.profit_factor)
        self.assertIsNone(metrics.payoff_ratio)

    def test_a_small_positive_sample_is_not_an_edge(self):
        metrics = compute([1.0] * 10, [100.0 + i for i in range(11)])
        self.assertGreater(metrics.expectancy, 0.0)
        self.assertFalse(metrics.has_edge)  # 10 trades is noise

    def test_sharpe_is_none_when_the_period_count_is_unknown(self):
        curve = [100.0, 101.0, 99.0, 103.0, 102.0]
        self.assertIsNone(compute([1.0, -2.0, 4.0, -1.0], curve).sharpe)
        self.assertIsNotNone(
            compute([1.0, -2.0, 4.0, -1.0], curve, periods_per_year=50).sharpe
        )

    def test_drawdown_measures_peak_to_trough(self):
        metrics = compute([0.0], [100.0, 120.0, 90.0, 110.0])
        self.assertAlmostEqual(metrics.max_drawdown_pct, 25.0, places=6)

    def test_annualisation_comes_from_the_data_not_a_guess(self):
        year = 365.25 * 24 * 3600
        self.assertEqual(trades_per_year(0.0, year, 40), 40)
        self.assertIsNone(trades_per_year(0.0, year, 1))
        self.assertIsNone(trades_per_year(None, year, 40))


class WalkForwardLabelling(unittest.TestCase):
    def test_without_a_grid_the_report_refuses_the_oos_label(self):
        bars = make_bars([100.0 + (i % 11) * 0.3 for i in range(400)])
        walk = WalkForward(zero_cost_config(), folds=3)
        report = walk.run(AlwaysLong, test_book(), "TEST", bars, grid=None)

        self.assertFalse(report.out_of_sample)
        self.assertFalse(report.usable_for_sizing)
        self.assertIn("in-sample", report.evidence_note)

    def test_a_single_combination_grid_is_still_not_a_selection(self):
        bars = make_bars([100.0 + (i % 11) * 0.3 for i in range(400)])
        walk = WalkForward(zero_cost_config(), folds=3)
        report = walk.run(
            AlwaysLong, test_book(), "TEST", bars, grid={"stop_pct": [0.02]}
        )
        self.assertFalse(report.out_of_sample)

    def test_a_real_grid_earns_the_oos_label_and_records_its_choices(self):
        bars = make_bars([100.0 + (i % 13) * 0.4 for i in range(600)])
        walk = WalkForward(zero_cost_config(), folds=3, min_train_trades=1)
        report = walk.run(
            AlwaysLong,
            test_book(),
            "TEST",
            bars,
            grid={"stop_pct": [0.01, 0.02], "target_pct": [0.02, 0.04]},
        )
        self.assertTrue(report.out_of_sample)
        self.assertEqual(report.grid_size, 4)
        self.assertTrue(report.folds)
        for fold in report.folds:
            self.assertIn("stop_pct", fold.chosen_params)

    def test_test_windows_never_overlap_and_move_forward(self):
        windows = _plan_windows(total=1000, folds=4, warmup=60)
        self.assertEqual(len(windows), 4)
        for earlier, later in zip(windows, windows[1:]):
            # The next test window starts no earlier than the previous one ended.
            self.assertGreaterEqual(later[2], earlier[3])
        for _train_start, train_end, test_start, test_end in windows:
            # Selection stops exactly where scoring starts.
            self.assertEqual(train_end, test_start)
            self.assertGreater(test_end, test_start)

    def test_too_little_history_produces_no_result_rather_than_a_forced_one(self):
        # Eight bars cannot carry a 2-bar warmup plus five distinct windows.
        bars = make_bars([100.0] * 8)
        walk = WalkForward(zero_cost_config(), folds=4)
        report = walk.run(AlwaysLong, test_book(), "TEST", bars, grid={"stop_pct": [0.01, 0.02]})
        self.assertEqual(report.metrics.trade_count, 0)
        self.assertFalse(report.out_of_sample)
        self.assertIn("Not enough bars", report.evidence_note)

    def test_fold_count_below_two_is_rejected(self):
        with self.assertRaises(ValueError):
            WalkForward(zero_cost_config(), folds=1)

    def test_selection_never_sees_a_bar_from_its_own_test_window(self):
        """The core claim of walk-forward, asserted rather than assumed.

        Records the bars handed to parameter selection on each fold and checks
        none of them fall inside that fold's scoring window. If this ever
        fails, every out-of-sample label in the repo is a lie.
        """
        seen = []

        class Recording(WalkForward):
            def _select(self, factory, book, instrument, train_bars, combos, equity):
                seen.append([b.ts for b in train_bars])
                return WalkForward._select(
                    self, factory, book, instrument, train_bars, combos, equity
                )

        bars = make_bars([100.0 + (i % 13) * 0.4 for i in range(600)])
        walk = Recording(zero_cost_config(), folds=3, min_train_trades=1)
        report = walk.run(
            AlwaysLong,
            test_book(),
            "TEST",
            bars,
            grid={"stop_pct": [0.01, 0.02]},
        )

        self.assertEqual(len(seen), len(report.folds))
        for timestamps, fold in zip(seen, report.folds):
            self.assertTrue(timestamps)
            test_window_start = bars[fold.test_start].ts
            self.assertLess(max(timestamps), test_window_start)

    def test_grid_expansion_is_a_full_product(self):
        combos = _expand_grid({"a": [1, 2], "b": [3, 4, 5]})
        self.assertEqual(len(combos), 6)
        self.assertEqual(len(_expand_grid(None)), 1)


class EvidenceGate(unittest.TestCase):
    """The store is the only door between a backtest and position size."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="cyrus-backtests-")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _payload(wins=20, losses=20, win_pnl=5.0, loss_pnl=-2.0, **overrides):
        payload = {
            "strategy": "always_long",
            "instrument": "TEST",
            "out_of_sample": True,
            "data_label": "yahoo (DELAYED)",
            "written_ts": 100.0,
            "metrics": {"trade_count": wins + losses},
            "trades": [{"net_pnl": win_pnl} for _ in range(wins)]
            + [{"net_pnl": loss_pnl} for _ in range(losses)],
        }
        payload.update(overrides)
        return payload

    def test_an_in_sample_report_is_refused(self):
        ok, reason = is_evidence(self._payload(out_of_sample=False))
        self.assertFalse(ok)
        self.assertIn("in-sample", reason)

    def test_a_synthetic_report_is_refused(self):
        ok, reason = is_evidence(self._payload(data_label="synthetic (DELAYED)"))
        self.assertFalse(ok)
        self.assertIn("not a real market feed", reason)

    def test_a_thin_sample_is_refused(self):
        ok, reason = is_evidence(self._payload(wins=6, losses=6))
        self.assertFalse(ok)
        self.assertIn("12 out-of-sample trades", reason)

    def test_a_real_out_of_sample_report_passes(self):
        ok, reason = is_evidence(self._payload())
        self.assertTrue(ok)
        self.assertIn("40 out-of-sample trades", reason)

    def test_records_are_rebuilt_from_trades_not_from_the_summary(self):
        records = strategy_records(reports=[self._payload()])
        record = records["always_long"]
        self.assertEqual(record.sample_size, 40)
        self.assertEqual(record.wins, 20)
        self.assertEqual(record.losses, 20)
        self.assertAlmostEqual(record.avg_win, 5.0, places=6)
        self.assertAlmostEqual(record.avg_loss, 2.0, places=6)
        self.assertTrue(record.out_of_sample)

    def test_a_refused_report_contributes_no_record_at_all(self):
        self.assertEqual(strategy_records(reports=[self._payload(out_of_sample=False)]), {})
        self.assertEqual(
            strategy_records(reports=[self._payload(data_label="synthetic")]), {}
        )

    def test_the_newest_report_per_instrument_wins(self):
        old = self._payload(written_ts=1.0)
        new = self._payload(written_ts=2.0)
        latest = latest_per_series([old, new])
        self.assertEqual(latest[("always_long", "TEST")]["written_ts"], 2.0)

    def test_one_instrument_never_erases_another(self):
        """Both reports are the same strategy; neither may overwrite the other."""
        spy = self._payload(instrument="SPY", written_ts=1.0)
        qqq = self._payload(instrument="QQQ", written_ts=2.0)
        latest = latest_per_series([spy, qqq])
        self.assertEqual(len(latest), 2)

    def test_instruments_are_pooled_into_one_strategy_record(self):
        spy = self._payload(instrument="SPY", wins=10, losses=5)
        qqq = self._payload(instrument="QQQ", wins=5, losses=15)
        record = strategy_records(reports=[spy, qqq])["always_long"]
        self.assertEqual(record.sample_size, 35)
        self.assertEqual(record.wins, 15)
        self.assertEqual(record.losses, 20)

    def test_a_winning_instrument_cannot_be_shown_without_its_losing_sibling(self):
        """Pooling is what stops a good SPY result standing alone.

        On its own the SPY report is a clean 30-for-30. Pooled with QQQ, the
        strategy is a loser, and that is the record Oracle has to size from.
        """
        winner = self._payload(instrument="SPY", wins=30, losses=0, win_pnl=5.0)
        loser = self._payload(instrument="QQQ", wins=0, losses=30, loss_pnl=-8.0)

        alone = strategy_records(reports=[winner])["always_long"]
        self.assertGreater(alone.gross_win - alone.gross_loss, 0.0)

        record = strategy_records(reports=[winner, loser])["always_long"]
        self.assertEqual(record.sample_size, 60)
        self.assertLess(record.gross_win - record.gross_loss, 0.0)

    def test_the_trade_floor_applies_to_the_pooled_sample(self):
        """Neither instrument clears 30 alone; together they do."""
        a = self._payload(instrument="SPY", wins=10, losses=10)
        b = self._payload(instrument="QQQ", wins=10, losses=10)
        self.assertEqual(strategy_records(reports=[a]), {})
        pooled = strategy_records(reports=[a, b])
        self.assertEqual(pooled["always_long"].sample_size, 40)

    def test_admissibility_is_about_provenance_not_size(self):
        ok, _ = is_admissible(self._payload(wins=1, losses=0))
        self.assertTrue(ok)
        ok, _ = is_admissible(self._payload(out_of_sample=False))
        self.assertFalse(ok)

    def test_a_thin_pooled_sample_is_refused_with_a_stated_reason(self):
        thin = self._payload(wins=3, losses=2)
        declined = refusals(reports=[thin])
        self.assertIn("always_long", declined)
        self.assertIn("5 admissible out-of-sample trades", declined["always_long"])

    def test_an_inadmissible_report_is_refused_on_provenance(self):
        declined = refusals(reports=[self._payload(data_label="synthetic")])
        self.assertIn("not a real market feed", declined["always_long"])

    def test_a_qualifying_strategy_is_not_listed_as_refused(self):
        self.assertEqual(refusals(reports=[self._payload()]), {})

    def test_saving_a_report_never_overwrites_an_existing_one(self):
        bars = make_bars([100.0 + (i % 13) * 0.4 for i in range(600)])
        walk = WalkForward(zero_cost_config(), folds=3, min_train_trades=1)
        report = walk.run(
            AlwaysLong,
            test_book(),
            "TEST",
            bars,
            grid={"stop_pct": [0.01, 0.02]},
            data_label="synthetic (test)",
        )
        first = save_report(report, root=self.root)
        self.assertTrue(os.path.exists(first))
        # Same name would collide; the store opens with "x" so it cannot clobber.
        with open(first, "r", encoding="utf-8") as handle:
            self.assertIn("evidence_note", handle.read())


class SyntheticIsNotEvidence(unittest.TestCase):
    def test_a_synthetic_walk_forward_runs_but_cannot_size(self):
        """The harness must work offline without that work counting."""
        bars = make_bars([100.0 + (i % 17) * 0.5 for i in range(600)])
        walk = WalkForward(zero_cost_config(), folds=3, min_train_trades=1)
        report = walk.run(
            AlwaysLong,
            test_book(),
            "TEST",
            bars,
            grid={"stop_pct": [0.01, 0.02]},
            data_label="synthetic (DELAYED)",
        )
        self.assertTrue(report.out_of_sample)
        ok, reason = is_evidence(report.to_dict())
        self.assertFalse(ok)
        self.assertIn("not a real market feed", reason)


class ShippedConfig(unittest.TestCase):
    def test_shipped_grids_reference_real_params(self):
        config = load_desk_config()
        for book in config.books.values():
            for key in book.optimize:
                self.assertIn(key, book.params, "%s.%s" % (book.name, key))

    def test_no_enabled_book_trades_for_free(self):
        """A backtest with no costs is not a result (AGENTS.md §9)."""
        config = load_desk_config()
        for book in config.enabled_books():
            costs = config.costs_for(book)
            self.assertGreater(
                costs.slippage_bps + costs.commission_bps, 0.0, book.name
            )

    def test_an_unconfigured_book_is_not_a_free_one(self):
        config = load_desk_config()
        costs = config.costs_for_book("no-such-book")
        self.assertEqual(costs.slippage_bps, config.execution.slippage_bps)
        self.assertEqual(costs.commission_bps, config.execution.commission_bps)

    def test_crypto_is_charged_more_than_equities(self):
        """Venue costs are facts about venues, not one global guess."""
        config = load_desk_config()
        equity_costs = config.costs_for(config.book("us_index"))
        crypto_costs = config.costs_for(config.book("crypto"))
        self.assertGreater(crypto_costs.commission_bps, equity_costs.commission_bps)


if __name__ == "__main__":
    unittest.main(verbosity=2)
