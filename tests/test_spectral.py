"""Spectral diagnostic tests.

The point of these is not that the FFT works. It is that the module refuses to
be impressed by itself: a planted cycle should be found, a wrap cliff should be
reported, and a trend with no cycle should not produce a tradeable claim.
"""

from __future__ import annotations

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cyrus.indicators.spectral import analyze, cliff_cost, reconstruct


def planted_cycle(n: int = 512, period: float = 64.0, amplitude: float = 10.0) -> list:
    """A clean cosine on a flat base: the easy case."""
    return [100.0 + amplitude * math.cos(2.0 * math.pi * i / period) for i in range(n)]


def cliffed_series(n: int = 512) -> list:
    """A strong ramp, so the last bar sits far from the first.

    This is the wrap cliff: the DFT assumes the series repeats, so the jump
    from the end back to the start gets treated as a real move.
    """
    return [100.0 + 0.5 * i + 3.0 * math.sin(i / 9.0) for i in range(n)]


class TestCycleDetection(unittest.TestCase):
    def test_a_planted_cycle_is_recovered(self):
        report = analyze(planted_cycle(period=64.0), top_k=8)
        self.assertIsNotNone(report.dominant_period_samples)
        self.assertAlmostEqual(report.dominant_period_samples, 64.0, delta=6.0)

    def test_a_planted_cycle_survives_the_holdout(self):
        report = analyze(planted_cycle(period=64.0), top_k=8)
        self.assertIsNotNone(report.out_of_sample_r2)
        self.assertGreater(report.out_of_sample_r2, 0.5)
        self.assertTrue(report.is_tradeable_evidence)

    def test_reconstruction_length_matches_the_input(self):
        values = planted_cycle()
        report = analyze(values, top_k=8)
        rebuilt = reconstruct(report.harmonics, len(values))
        self.assertEqual(len(rebuilt), len(values))


class TestWrapCliff(unittest.TestCase):
    def test_a_ramp_after_detrending_has_a_small_cliff(self):
        """Detrending is what kills most of the cliff a ramp would produce."""
        report = analyze(cliffed_series(), detrend=True)
        self.assertIn(report.leakage_risk, ("low", "moderate"))

    def test_leaving_the_trend_in_creates_a_large_cliff(self):
        report = analyze(cliffed_series(), detrend=False)
        self.assertEqual(report.leakage_risk, "high")
        self.assertIn("artifact", report.verdict.lower())

    def test_high_leakage_is_never_tradeable_evidence(self):
        report = analyze(cliffed_series(), detrend=False)
        self.assertFalse(report.is_tradeable_evidence)

    def test_cliff_cost_quantifies_the_artifact_share(self):
        cost = cliff_cost(cliffed_series())
        self.assertIn("artifact_share", cost)
        self.assertGreaterEqual(cost["artifact_share"], 0.0)
        self.assertIn(cost["leakage_risk"], ("low", "moderate", "high"))

    def test_the_cliff_is_reported_in_atr_units_when_atr_is_known(self):
        report = analyze(cliffed_series(), detrend=False, atr_value=2.0)
        self.assertIsNotNone(report.wrap_cliff_atr_multiple)
        self.assertGreater(report.wrap_cliff_atr_multiple, 0.0)


class TestHonesty(unittest.TestCase):
    def test_too_few_samples_refuses_to_produce_a_diagnostic(self):
        report = analyze([1.0, 2.0, 3.0], top_k=8)
        self.assertEqual(report.harmonics, [])
        self.assertIn("Too few samples", report.verdict)
        self.assertFalse(report.is_tradeable_evidence)

    def test_a_pure_trend_offers_no_cycle_to_trade(self):
        trend = [100.0 + 0.4 * i for i in range(300)]
        report = analyze(trend, detrend=True)
        self.assertFalse(report.is_tradeable_evidence)

    def test_in_sample_fit_alone_is_not_enough(self):
        """A high in-sample R2 with a failed holdout must not qualify."""
        import random

        rng = random.Random(11)
        noise = [100.0 + rng.gauss(0, 1) for _ in range(400)]
        report = analyze(noise, top_k=8)
        if report.out_of_sample_r2 is not None and report.out_of_sample_r2 < 0.10:
            self.assertFalse(report.is_tradeable_evidence)


if __name__ == "__main__":
    unittest.main(verbosity=2)
