"""Paired-difference CI + +-5% margin verdict tests on synthetic data."""

from __future__ import annotations

import numpy as np

from analysis.paired import paired_difference


def test_tie_when_within_margin():
    rng = np.random.default_rng(1)
    dotnet = 100.0 + rng.normal(0, 1.0, size=12)
    go = dotnet + rng.normal(0, 0.5, size=12)  # ~0% difference
    r = paired_difference(go, dotnet, metric="cpu_ms_per_req", margin_pct=5.0)
    assert r.is_tie
    assert -5.0 <= r.ci_low_pct and r.ci_high_pct <= 5.0


def test_verdict_method_is_always_t():
    # Pre-registered: the verdict path is the paired-t interval unconditionally,
    # never auto-switched to bootstrap on a normality screen.
    rng = np.random.default_rng(7)
    dotnet = 100.0 + rng.normal(0, 1.0, size=12)
    go = dotnet + rng.normal(0, 0.5, size=12)
    r = paired_difference(go, dotnet, metric="cpu_ms_per_req")
    assert r.method == "t"


def test_diagnostics_reported_but_off_verdict_path():
    # Shapiro p + bootstrap interval are surfaced as diagnostics, and the verdict
    # CI is the t-interval (distinct from the reported bootstrap interval object).
    rng = np.random.default_rng(8)
    dotnet = 100.0 + rng.normal(0, 1.0, size=12)
    go = 90.0 + rng.normal(0, 1.0, size=12)
    r = paired_difference(go, dotnet, metric="cpu_ms_per_req")
    assert r.shapiro_p is not None and 0.0 <= r.shapiro_p <= 1.0
    # bootstrap interval is populated and brackets the mean diff %
    assert r.boot_ci_low_pct <= r.mean_diff_pct <= r.boot_ci_high_pct
    # verdict CI is the t-interval; it is what the margin test uses
    assert r.ci_low_pct <= r.mean_diff_pct <= r.ci_high_pct


def test_go_better_when_clearly_lower_cost():
    rng = np.random.default_rng(2)
    dotnet = 100.0 + rng.normal(0, 1.0, size=12)
    go = 80.0 + rng.normal(0, 1.0, size=12)  # ~20% lower (better for cost)
    r = paired_difference(go, dotnet, metric="cpu_ms_per_req", lower_is_better=True)
    assert r.verdict == "go_better"
    assert r.ci_high_pct < -5.0  # entirely below the lower margin


def test_dotnet_better_when_go_higher_cost():
    rng = np.random.default_rng(4)
    dotnet = 100.0 + rng.normal(0, 1.0, size=12)
    go = 120.0 + rng.normal(0, 1.0, size=12)
    r = paired_difference(go, dotnet, metric="cpu_ms_per_req", lower_is_better=True)
    assert r.verdict == "dotnet_better"


def test_throughput_direction_flips():
    # For a higher-is-better metric (knee RPS), Go higher => Go better.
    rng = np.random.default_rng(5)
    dotnet = 1000.0 + rng.normal(0, 5.0, size=12)
    go = 1200.0 + rng.normal(0, 5.0, size=12)
    r = paired_difference(go, dotnet, metric="knee_rps", lower_is_better=False)
    assert r.verdict == "go_better"


def test_pairing_alignment_required():
    import pytest

    with pytest.raises(ValueError):
        paired_difference([1.0, 2.0], [1.0], metric="x")


def test_zero_variance_diffs_use_t_path_with_collapsed_ci():
    # Constant offset => zero-variance diffs => Shapiro undefined (None) and the
    # t-interval collapses to the point estimate; verdict still falls out cleanly.
    go = [110.0] * 10
    dotnet = [100.0] * 10
    r = paired_difference(go, dotnet, metric="cpu_ms_per_req")
    assert r.method == "t"
    assert r.shapiro_p is None  # undefined on zero-variance diffs
    assert abs(r.mean_diff_pct - 10.0) < 1e-9
    assert abs(r.ci_low_pct - 10.0) < 1e-9 and abs(r.ci_high_pct - 10.0) < 1e-9
    assert r.verdict == "dotnet_better"  # Go is 10% higher cost


def test_effect_size_reported():
    rng = np.random.default_rng(6)
    dotnet = 100.0 + rng.normal(0, 1.0, size=20)
    go = 90.0 + rng.normal(0, 1.0, size=20)
    r = paired_difference(go, dotnet, metric="cpu_ms_per_req")
    assert abs(r.effect_size) > 1.0  # large, well-separated effect
