"""Spectral diagnostics: x(t) = sum_k A_k * cos(2*pi*f_k*t + rho_k).

Every chart is a sum of circles, and that is exactly why this module is a
diagnostic and never a trigger. The DFT assumes the series repeats. A real
price series does not: the jump between the last bar and the first bar is a
discontinuity, and the transform pays for that cliff by inventing power at
frequencies that are not in the market.

So this module always reports the cliff, always offers a window, and always
supports an out-of-sample check. A perfect in-sample reconstruction is
evidence of nothing.

Oracle may cite these numbers as context. No proposal may use them as its
entry reason.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

import numpy as np


@dataclass
class Harmonic:
    """One spinning vector: amplitude, frequency, phase."""

    index: int
    amplitude: float
    frequency: float          # cycles per sample
    period_samples: float     # 1 / frequency
    phase: float              # radians
    power_fraction: float     # share of total spectral power

    def value_at(self, t: float) -> float:
        return self.amplitude * math.cos(2.0 * math.pi * self.frequency * t + self.phase)


@dataclass
class SpectralReport:
    """What the spectrum says, and how much of it to believe."""

    n_samples: int
    detrended: bool
    window: str
    wrap_cliff: float                 # |last - first| after detrending
    wrap_cliff_atr_multiple: Optional[float]
    leakage_risk: str                 # low | moderate | high
    harmonics: List[Harmonic] = field(default_factory=list)
    dominant_period_samples: Optional[float] = None
    dominant_power_fraction: Optional[float] = None
    in_sample_r2: Optional[float] = None
    out_of_sample_r2: Optional[float] = None
    verdict: str = ""

    @property
    def is_tradeable_evidence(self) -> bool:
        """Only true when a cycle survives a holdout and the cliff is small.

        Even then it is a tilt on an existing setup, not a standalone entry.
        """
        if self.out_of_sample_r2 is None:
            return False
        return self.out_of_sample_r2 >= 0.10 and self.leakage_risk != "high"


def _hann(n: int) -> np.ndarray:
    if n <= 1:
        return np.ones(n)
    return 0.5 - 0.5 * np.cos(2.0 * math.pi * np.arange(n) / (n - 1))


def _detrend(values: np.ndarray) -> np.ndarray:
    """Remove the least-squares line. A trend masquerades as a giant cycle."""
    n = len(values)
    t = np.arange(n, dtype=float)
    slope, intercept = np.polyfit(t, values, 1)
    return values - (slope * t + intercept)


def analyze(
    values: Sequence[float],
    top_k: int = 8,
    detrend: bool = True,
    window: str = "hann",
    atr_value: Optional[float] = None,
    holdout_fraction: float = 0.25,
) -> SpectralReport:
    """Decompose a series and grade how much of the spectrum to trust.

    ``top_k`` defaults to 8: eight spinning vectors whose tip redraws the price.
    ``atr_value`` lets the wrap cliff be expressed in units the desk already
    reasons in, which is how you tell a cosmetic discontinuity from a real one.
    """
    series = np.asarray(list(values), dtype=float)
    n = len(series)
    if n < 16:
        return SpectralReport(
            n_samples=n,
            detrended=False,
            window="none",
            wrap_cliff=0.0,
            wrap_cliff_atr_multiple=None,
            leakage_risk="high",
            verdict="Too few samples for a spectrum. No diagnostic produced.",
        )

    work = _detrend(series) if detrend else series - series.mean()

    # The cliff: what the transform will pretend is a real move.
    cliff = float(abs(work[-1] - work[0]))
    cliff_atr = float(cliff / atr_value) if atr_value and atr_value > 0 else None
    spread = float(np.max(work) - np.min(work)) or 1.0
    cliff_ratio = cliff / spread
    if cliff_ratio > 0.35:
        leakage = "high"
    elif cliff_ratio > 0.15:
        leakage = "moderate"
    else:
        leakage = "low"

    taper = _hann(n) if window == "hann" else np.ones(n)
    windowed = work * taper

    spectrum = np.fft.rfft(windowed)
    freqs = np.fft.rfftfreq(n, d=1.0)
    power = np.abs(spectrum) ** 2
    power[0] = 0.0  # the mean is not a cycle
    total_power = float(power.sum()) or 1.0

    # Compensate the amplitude for energy the window removed.
    window_gain = float(taper.mean()) or 1.0
    order = np.argsort(power)[::-1][:top_k]

    harmonics: List[Harmonic] = []
    for idx in sorted(order.tolist()):
        if freqs[idx] <= 0:
            continue
        amplitude = float(2.0 * np.abs(spectrum[idx]) / (n * window_gain))
        harmonics.append(
            Harmonic(
                index=int(idx),
                amplitude=amplitude,
                frequency=float(freqs[idx]),
                period_samples=float(1.0 / freqs[idx]),
                phase=float(np.angle(spectrum[idx])),
                power_fraction=float(power[idx] / total_power),
            )
        )

    harmonics.sort(key=lambda h: h.power_fraction, reverse=True)
    dominant = harmonics[0] if harmonics else None

    in_sample_r2 = _r2(work, reconstruct(harmonics, n))

    out_of_sample_r2: Optional[float] = None
    if 0.0 < holdout_fraction < 0.5:
        split = int(n * (1.0 - holdout_fraction))
        if split >= 16 and n - split >= 8:
            train = analyze(
                series[:split],
                top_k=top_k,
                detrend=detrend,
                window=window,
                atr_value=atr_value,
                holdout_fraction=0.0,
            )
            # Continue the fitted circles forward into unseen bars.
            future = np.array(
                [sum(h.value_at(float(t)) for h in train.harmonics) for t in range(split, n)]
            )
            out_of_sample_r2 = _r2(work[split:], future)

    return SpectralReport(
        n_samples=n,
        detrended=detrend,
        window=window,
        wrap_cliff=cliff,
        wrap_cliff_atr_multiple=cliff_atr,
        leakage_risk=leakage,
        harmonics=harmonics,
        dominant_period_samples=dominant.period_samples if dominant else None,
        dominant_power_fraction=dominant.power_fraction if dominant else None,
        in_sample_r2=in_sample_r2,
        out_of_sample_r2=out_of_sample_r2,
        verdict=_verdict(leakage, in_sample_r2, out_of_sample_r2, dominant),
    )


def reconstruct(harmonics: Sequence[Harmonic], n: int) -> np.ndarray:
    """Sum the circles back into a series. The tip redraws the price."""
    t = np.arange(n, dtype=float)
    out = np.zeros(n, dtype=float)
    for h in harmonics:
        out += h.amplitude * np.cos(2.0 * math.pi * h.frequency * t + h.phase)
    return out


def cliff_cost(values: Sequence[float], top_k: int = 8) -> dict:
    """How much of the dominant cycle exists only because of the wrap cliff.

    Compares the unwindowed spectrum against the windowed one. When killing
    the cliff removes most of the dominant cycle's power, that cycle was an
    artifact of the transform, not a property of the market.
    """
    raw = analyze(values, top_k=top_k, window="none", holdout_fraction=0.0)
    tapered = analyze(values, top_k=top_k, window="hann", holdout_fraction=0.0)
    raw_power = raw.dominant_power_fraction or 0.0
    taper_power = tapered.dominant_power_fraction or 0.0
    lost = raw_power - taper_power
    return {
        "wrap_cliff": raw.wrap_cliff,
        "leakage_risk": raw.leakage_risk,
        "dominant_power_unwindowed": raw_power,
        "dominant_power_windowed": taper_power,
        "power_lost_to_cliff": lost,
        "artifact_share": (lost / raw_power) if raw_power > 0 else 0.0,
        "dominant_period_unwindowed": raw.dominant_period_samples,
        "dominant_period_windowed": tapered.dominant_period_samples,
    }


def _r2(actual: np.ndarray, predicted: np.ndarray) -> Optional[float]:
    if len(actual) == 0 or len(actual) != len(predicted):
        return None
    residual = float(np.sum((actual - predicted) ** 2))
    variance = float(np.sum((actual - np.mean(actual)) ** 2))
    if variance <= 1e-12:
        return None
    return 1.0 - residual / variance


def _verdict(
    leakage: str,
    in_sample_r2: Optional[float],
    out_of_sample_r2: Optional[float],
    dominant: Optional[Harmonic],
) -> str:
    if dominant is None:
        return "No dominant cycle. Treat the series as trend plus noise."
    period = "%.0f samples" % dominant.period_samples
    power = "%.0f%% power" % (dominant.power_fraction * 100.0)
    if leakage == "high":
        return (
            "Dominant %s at %s, but the wrap cliff is large. Most of this cycle is "
            "likely a transform artifact. Do not size on it." % (period, power)
        )
    if out_of_sample_r2 is None:
        return "Dominant %s at %s, in-sample only. No holdout evidence." % (period, power)
    if out_of_sample_r2 < 0:
        return (
            "Dominant %s at %s fits in sample and fails out of sample "
            "(R2 %.2f). Discard." % (period, power, out_of_sample_r2)
        )
    if out_of_sample_r2 < 0.10:
        return (
            "Dominant %s at %s survives weakly out of sample (R2 %.2f). "
            "Context only." % (period, power, out_of_sample_r2)
        )
    return (
        "Dominant %s at %s holds out of sample (R2 %.2f). Usable as a tilt on an "
        "existing setup, never as the entry reason." % (period, power, out_of_sample_r2)
    )


__all__ = ["Harmonic", "SpectralReport", "analyze", "reconstruct", "cliff_cost"]
