"""Paired-difference CI + the +-5% pre-registered margin verdict.

Go and .NET are paired BY INTERLEAVE BLOCK (RCB -> block_id is the pairing key,
spec/slo.yaml decision_rule: paired_difference_ci). For each headline metric
(knee RPS, CPU-ms/req, anon-RSS/req, $/M) we compute per-block paired differences
and a paired CI.

Pre-registered CI method (no researcher degrees of freedom): the VERDICT is
ALWAYS read off the paired-t interval on the per-block differences, regardless of
how the diffs look. We do NOT auto-switch t-vs-bootstrap on a normality screen:
that switch is data-dependent (a forking path) AND the small-n percentile
bootstrap under-covers at n~10 (small-n bootstrap CIs are unreliable). The
paired t-interval is the pre-registered headline rule; at n>=10
interleaved reps it is robust to the mild non-normality these diffs show.

Shapiro-Wilk and the percentile bootstrap are still computed, but ONLY as
REPORTED DIAGNOSTICS (`shapiro_p`, `boot_ci_low_pct`, `boot_ci_high_pct`); they
never gate the verdict. They let a reader see whether the normality assumption is
strained and how a bootstrap would have moved the interval, without letting that
inspection change the pre-registered decision.

The margin (spec/slo.yaml equivalence): +-5%, RELATIVE, expressed against the
.NET (reference) mean, so the verdict is in the same percent units the headline
reports. t-CI entirely inside +-5% -> TIE; outside -> report effect size +
direction. No CI-overlap eyeballing.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import stats


@dataclass(frozen=True)
class PairedResult:
    metric: str
    n_blocks: int
    mean_go: float
    mean_dotnet: float
    mean_diff: float            # go - dotnet (absolute units)
    mean_diff_pct: float        # 100 * (go - dotnet) / dotnet
    ci_low_pct: float           # paired-t interval (THE verdict path)
    ci_high_pct: float
    margin_pct: float           # pre-registered equivalence band (+-), NOT the CI level
    ci_pct: float               # CONFIDENCE LEVEL of ci_low/high_pct (e.g. 95.0)
    method: str                 # always "t" (pre-registered); kept for the report
    verdict: str                # "tie" | "go_better" | "dotnet_better"
    effect_size: float          # Cohen's d_z on the paired diffs (None-safe -> 0)
    # --- reported diagnostics only (NOT on the verdict path) ---------------- #
    shapiro_p: float | None     # Shapiro-Wilk p on the diffs (None if undefined)
    boot_ci_low_pct: float      # percentile-bootstrap interval, for comparison
    boot_ci_high_pct: float

    @property
    def is_tie(self) -> bool:
        return self.verdict == "tie"


def _shapiro_p(diffs: np.ndarray) -> float | None:
    """Shapiro-Wilk p on the paired diffs, REPORTED-ONLY diagnostic.

    Returns None where the test is undefined (n<3 or zero-variance diffs). This
    value never gates the verdict; it is surfaced so a reader can judge how
    strained the t-interval's normality assumption is.
    """
    if diffs.size < 3:
        return None
    if np.allclose(diffs, diffs[0]):
        return None  # zero variance -> Shapiro undefined
    try:
        _, p = stats.shapiro(diffs)
    except Exception:
        return None
    return float(p)


def _t_ci_pct(diffs: np.ndarray, ref_mean: float, ci: float) -> tuple[float, float]:
    n = diffs.size
    mean = float(np.mean(diffs))
    se = float(stats.sem(diffs)) if n > 1 else 0.0
    if se == 0.0:
        return (100.0 * mean / ref_mean,) * 2
    tcrit = float(stats.t.ppf(1.0 - (1.0 - ci) / 2.0, df=n - 1))
    lo = mean - tcrit * se
    hi = mean + tcrit * se
    return 100.0 * lo / ref_mean, 100.0 * hi / ref_mean


def _bootstrap_ci_pct(
    diffs: np.ndarray, ref_mean: float, ci: float, *, n_boot: int, seed: int
) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    n = diffs.size
    means = np.array(
        [float(np.mean(rng.choice(diffs, size=n, replace=True))) for _ in range(n_boot)]
    )
    alpha = (1.0 - ci) / 2.0
    lo = float(np.quantile(means, alpha))
    hi = float(np.quantile(means, 1.0 - alpha))
    return 100.0 * lo / ref_mean, 100.0 * hi / ref_mean


def paired_difference(
    go: list[float] | np.ndarray,
    dotnet: list[float] | np.ndarray,
    *,
    metric: str,
    margin_pct: float = 5.0,
    lower_is_better: bool = True,
    ci: float = 0.95,
    n_boot: int = 5000,
    seed: int = 12345,
) -> PairedResult:
    """Paired CI of (go - dotnet) as a percent of the .NET mean + margin verdict.

    `go[i]` and `dotnet[i]` are the metric values for the SAME interleave block i
    (already aligned by block_id). `lower_is_better` flips the direction word for
    cost/latency metrics (lower = "faster"/"cheaper") vs throughput metrics
    (higher = better) so the verdict reads correctly.

    The VERDICT is read off the paired-t interval UNCONDITIONALLY (pre-registered;
    see module docstring). Shapiro-Wilk (`shapiro_p`) and the percentile bootstrap
    (`boot_ci_*_pct`) are computed and returned as diagnostics only and never
    change the decision.
    """
    g = np.asarray(go, dtype=np.float64)
    d = np.asarray(dotnet, dtype=np.float64)
    if g.shape != d.shape:
        raise ValueError("go and dotnet must be aligned (same number of blocks)")
    if g.size == 0:
        raise ValueError("no paired blocks")
    diffs = g - d
    ref_mean = float(np.mean(d))
    if ref_mean == 0.0:
        raise ValueError("reference (.NET) mean is zero; percent margin undefined")

    mean_diff = float(np.mean(diffs))
    mean_diff_pct = 100.0 * mean_diff / ref_mean

    # Pre-registered verdict path: paired-t interval, always.
    lo_pct, hi_pct = _t_ci_pct(diffs, ref_mean, ci)

    # Reported diagnostics only (do NOT feed the verdict).
    shapiro_p = _shapiro_p(diffs)
    boot_lo_pct, boot_hi_pct = _bootstrap_ci_pct(diffs, ref_mean, ci, n_boot=n_boot, seed=seed)

    sd = float(np.std(diffs, ddof=1)) if diffs.size > 1 else 0.0
    effect = (mean_diff / sd) if sd > 0 else 0.0

    inside = lo_pct >= -margin_pct and hi_pct <= margin_pct
    if inside:
        verdict = "tie"
    else:
        go_lower = mean_diff < 0
        if lower_is_better:
            verdict = "go_better" if go_lower else "dotnet_better"
        else:
            verdict = "dotnet_better" if go_lower else "go_better"

    return PairedResult(
        metric=metric, n_blocks=int(g.size),
        mean_go=float(np.mean(g)), mean_dotnet=ref_mean,
        mean_diff=mean_diff, mean_diff_pct=mean_diff_pct,
        ci_low_pct=lo_pct, ci_high_pct=hi_pct,
        margin_pct=margin_pct, ci_pct=100.0 * ci, method="t", verdict=verdict,
        effect_size=effect,
        shapiro_p=shapiro_p,
        boot_ci_low_pct=boot_lo_pct, boot_ci_high_pct=boot_hi_pct,
    )
