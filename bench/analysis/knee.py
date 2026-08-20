"""Knee fit: isotonic p99-vs-rate -> SLO crossing + bootstrap-over-reps CI.

Knee = the offered rate at which the isotonic (PAVA) fit of p99-vs-rate crosses
the SLO (p99 = 20 ms, spec/slo.yaml). CI on the knee comes from bootstrapping over
the interleaved reps (between-run resampling), refitting isotonic on each resample
and reading the rate-at-SLO each time -> percentile CI.

The percentile-bootstrap CI is statistically defensible ONLY at the >=10-rep
CONFIRM stage (spec/slo.yaml ladder.confirm_reps: 10; n=5 is too few for a
between-run bootstrap CI). The exploratory LADDER runs only ladder.reps_per_rung: 3 reps/rung, far
too few for a between-run bootstrap CI. `estimate_knee` therefore takes a
`min_reps` guard (default 10): if any rung in the SLO-crossing bracket carries
fewer reps than that, the returned `KneeEstimate` is flagged `ci_valid=False` with
a `warning`, and ladder-stage callers MUST treat the result as POINT-ESTIMATE-ONLY
(the CI bounds are still reported for transparency but are not inferential). We do
NOT silently widen or fabricate a CI for under-repped data.

Input model: a `LadderData` of rungs, each rung = an offered rate with a list of
per-rep p99 values (one per interleaved rep). All pure; deterministic given the
bootstrap `seed`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .pava import isotonic_fit


@dataclass
class Rung:
    rate_rps: float
    p99_ms_by_rep: list[float] = field(default_factory=list)

    def mean_p99(self) -> float:
        return float(np.mean(self.p99_ms_by_rep)) if self.p99_ms_by_rep else float("nan")


@dataclass
class LadderData:
    rungs: list[Rung] = field(default_factory=list)

    def sorted_rungs(self) -> list[Rung]:
        return sorted(self.rungs, key=lambda r: r.rate_rps)

    def n_reps(self) -> int:
        """Min rep count across rungs (the bootstrap resamples within each rung)."""
        return min((len(r.p99_ms_by_rep) for r in self.rungs), default=0)


@dataclass(frozen=True)
class KneeEstimate:
    knee_rps: float
    ci_low_rps: float
    ci_high_rps: float
    slo_ms: float
    n_boot: int
    crossed: bool         # False if the fit never reaches the SLO in-range
    ci_valid: bool = True            # False => point-estimate-only (under-repped)
    min_reps_in_bracket: int = 0     # min rep count across the crossing-bracket rungs
    warning: str | None = None       # set when ci_valid is False


def _crossing_rate(rates: np.ndarray, fitted: np.ndarray, slo_ms: float) -> float | None:
    """First rate at which the monotone `fitted` p99 reaches `slo_ms`.

    Linear interpolation between the two bracketing rungs. None if the fit stays
    below the SLO across the whole ladder (no knee in range -> sub-knee ladder).
    """
    if fitted[0] >= slo_ms:
        return float(rates[0])          # already at/over SLO at the lowest rung
    for i in range(1, len(rates)):
        if fitted[i] >= slo_ms:
            x0, x1 = rates[i - 1], rates[i]
            y0, y1 = fitted[i - 1], fitted[i]
            if y1 == y0:
                return float(x1)
            frac = (slo_ms - y0) / (y1 - y0)
            return float(x0 + frac * (x1 - x0))
    return None


def fit_knee_point(ladder: LadderData, slo_ms: float) -> float | None:
    """Point estimate of the knee from the mean-p99 isotonic fit."""
    rungs = ladder.sorted_rungs()
    rates = np.array([r.rate_rps for r in rungs], dtype=np.float64)
    y = np.array([r.mean_p99() for r in rungs], dtype=np.float64)
    fitted = isotonic_fit(y)
    return _crossing_rate(rates, fitted, slo_ms)


def _bracket_min_reps(rungs: list[Rung], fitted: np.ndarray, slo_ms: float) -> int:
    """Min rep count across the rungs bracketing the SLO crossing.

    The CI's validity hinges on the reps at the rungs that actually frame the
    crossing (the interpolation endpoints), not on far-away rungs. Returns the
    smaller of the two bracketing rungs' rep counts; for a crossing at/below the
    first rung, that single rung's count; 0 if there is no crossing in range.
    """
    if fitted[0] >= slo_ms:
        return len(rungs[0].p99_ms_by_rep)
    for i in range(1, len(rungs)):
        if fitted[i] >= slo_ms:
            return min(len(rungs[i - 1].p99_ms_by_rep), len(rungs[i].p99_ms_by_rep))
    return 0


def estimate_knee(
    ladder: LadderData,
    slo_ms: float = 20.0,
    *,
    n_boot: int = 2000,
    ci: float = 0.95,
    seed: int = 12345,
    min_reps: int = 10,
) -> KneeEstimate:
    """Knee point + bootstrap-over-reps percentile CI.

    Each bootstrap iteration resamples reps WITH REPLACEMENT independently per
    rung, refits isotonic on the resampled per-rung means, and records the SLO
    crossing. The CI is the percentile interval of those crossings.

    `min_reps` (default 10, = spec/slo.yaml ladder.confirm_reps) guards the CI:
    if either rung framing the SLO crossing carries fewer than `min_reps` reps
    (e.g. the 3-rep exploratory ladder), the returned estimate is flagged
    `ci_valid=False` with a `warning`, and the CI bounds are POINT-ESTIMATE-ONLY
    context, not an inferential interval. The bounds are still computed and
    reported (never faked or widened); only their interpretation is downgraded.
    """
    rungs = ladder.sorted_rungs()
    if len(rungs) < 2:
        raise ValueError("need at least 2 rungs to fit a knee")
    rates = np.array([r.rate_rps for r in rungs], dtype=np.float64)
    reps = [np.asarray(r.p99_ms_by_rep, dtype=np.float64) for r in rungs]
    if any(rp.size == 0 for rp in reps):
        raise ValueError("every rung needs at least one rep")

    point = fit_knee_point(ladder, slo_ms)

    # Reps at the rungs that frame the crossing decide whether the CI is valid.
    point_fitted = isotonic_fit(np.array([r.mean_p99() for r in rungs], dtype=np.float64))
    bracket_reps = _bracket_min_reps(rungs, point_fitted, slo_ms)
    ci_valid = bracket_reps >= min_reps
    warning = (
        None if ci_valid
        else (
            f"CI invalid: crossing bracket has {bracket_reps} rep(s) < min_reps={min_reps} "
            f"(ladder stage, reps_per_rung=3). Treat knee as point-estimate-only; "
            f"the >=10-rep confirm stage produces the inferential CI."
        )
    )

    rng = np.random.default_rng(seed)
    crossings: list[float] = []
    for _ in range(n_boot):
        means = np.empty(len(rungs), dtype=np.float64)
        for j, rp in enumerate(reps):
            sample = rng.choice(rp, size=rp.size, replace=True)
            means[j] = float(np.mean(sample))
        fitted = isotonic_fit(means)
        c = _crossing_rate(rates, fitted, slo_ms)
        if c is not None:
            crossings.append(c)

    crossed = point is not None
    if not crossings:
        # Fit never reaches the SLO in-range: ladder is entirely sub-knee. Report
        # the top rate as a lower bound rather than fabricating a crossing.
        top = float(rates[-1])
        return KneeEstimate(
            knee_rps=point if point is not None else top,
            ci_low_rps=top, ci_high_rps=top,
            slo_ms=slo_ms, n_boot=n_boot, crossed=False,
            ci_valid=ci_valid, min_reps_in_bracket=bracket_reps, warning=warning,
        )

    arr = np.array(crossings, dtype=np.float64)
    alpha = (1.0 - ci) / 2.0
    lo = float(np.quantile(arr, alpha))
    hi = float(np.quantile(arr, 1.0 - alpha))
    knee = point if point is not None else float(np.median(arr))
    return KneeEstimate(
        knee_rps=knee, ci_low_rps=lo, ci_high_rps=hi,
        slo_ms=slo_ms, n_boot=n_boot, crossed=crossed,
        ci_valid=ci_valid, min_reps_in_bracket=bracket_reps, warning=warning,
    )
