"""Capacity-gate lever logic + headroom rule (spec/slo.yaml)."""

from __future__ import annotations

import pytest

from ledgerbench import config
from ledgerbench.capacity_gate import (
    MIN_PG_KNEE_RUNGS,
    LeverApplication,
    LeverMachine,
    ResolvedWorkloadParams,
    RungResult,
    Verdict,
    apply_params_to_workload,
    effective_pg_capacity_rps,
    effective_workload_record,
    evaluate_gate,
    format_cpuset,
    headroom_after_remeasure,
    headroom_ok,
    params_for_applied_levers,
    parse_cpuset,
    pg_cpuset_overrides,
    pg_knee_from_rungs,
    projected_db_load_rps,
    realize_levers,
    unmeasured_knee_result,
)


def _params():
    # Built from the real workload mix so the projection uses actual per-endpoint
    # weights (get 0.30, list 0.25, quote 0.20, create 0.15, pdf 0.08, healthz 0.02).
    return ResolvedWorkloadParams.from_workload(config.load_workload())


def test_projected_db_load_drops_with_levers():
    p = _params()
    base = projected_db_load_rps(10_000, p)
    # write_share lever 0.15 -> 0.10
    p.write_share = 0.10
    lower_write = projected_db_load_rps(10_000, p)
    assert lower_write < base
    # cache hit 0.80 -> 0.90 lowers read DB-touch
    p.cache_hit_rate = 0.90
    lower_cache = projected_db_load_rps(10_000, p)
    assert lower_cache < lower_write


def test_projection_uses_actual_mix_not_write_complement():
    # DB-touch = create(0.15) + list(0.25) + pdf(0.08) + get(0.30)*miss(0.20) = 0.54.
    # The old (1 - write_share)*miss read model would give a different number;
    # quote(0.20) and healthz(0.02) must NOT contribute (they never touch PG).
    p = _params()
    assert abs(projected_db_load_rps(10_000, p) - 5400.0) < 1e-6


def test_projection_excludes_non_db_endpoints():
    # Shifting quote/healthz share has no effect on projected DB load.
    p = _params()
    base = projected_db_load_rps(10_000, p)
    p.pricing_quote_share = 0.0
    p.healthz_share = 0.0
    assert projected_db_load_rps(10_000, p) == base


def test_headroom_rule():
    assert headroom_ok(pg_capacity_rps=1300, projected_db_rps=1000, headroom_factor=1.3)
    assert not headroom_ok(pg_capacity_rps=1299, projected_db_rps=1000, headroom_factor=1.3)


def test_pg_cores_lever_scales_capacity_linearly():
    p = _params()
    assert effective_pg_capacity_rps(3000, p) == 3000  # 3 cores -> unchanged
    p.pg_cores = 4
    assert effective_pg_capacity_rps(3000, p) == 4000  # 4/3 linear scale


def test_pg_cores_lever_can_rescue_a_db_imposed_case():
    # A case that FAILS headroom even after the write_share + cache_hit levers,
    # but PASSES once pg_cores 3->4 scales capacity, must NOT be declared
    # DB-imposed. This is the bug: previously pg_cores moved neither side.
    slo = config.load_slo()
    p = _params()
    projected_knee = 8000
    # need AFTER write+cache levers (the smallest requirement those two reach):
    after = ResolvedWorkloadParams.from_workload(config.load_workload())
    after.write_share = 0.10
    after.cache_hit_rate = 0.90
    need_after_soft = projected_db_load_rps(projected_knee, after) * slo.capacity_gate.headroom_factor
    # measured 3-core knee: below need_after_soft (soft levers can't pass) but its
    # 4/3 scale clears it (only the core bump rescues the case).
    measured = need_after_soft * 3.0 / 4.0 + 1.0
    assert measured < need_after_soft  # soft levers alone cannot pass
    res = evaluate_gate(
        pg_knee_rps=measured, projected_knee_rps=projected_knee,
        cfg=slo.capacity_gate, params=p,
    )
    assert res.verdict == Verdict.PASS
    assert "pg_cores" in {lv.name for lv in res.levers_fired}
    # the MEASURED knee is reported verbatim ...
    assert res.pg_knee_rps == pytest.approx(measured)
    assert res.pg_knee_measured_at_cores == 3
    # ... and the 4/3 core scaling lives in its OWN field, labeled a projection
    assert res.pg_capacity_projected_rps == pytest.approx(measured * 4 / 3)
    assert "LINEAR core-scaling assumption" in res.pg_capacity_projection_basis
    assert "NOT a measurement" in res.pg_capacity_projection_basis


def test_gate_passes_without_levers_when_capacity_high():
    slo = config.load_slo()
    res = evaluate_gate(
        pg_knee_rps=100_000, projected_knee_rps=8000,
        cfg=slo.capacity_gate, params=_params(),
    )
    assert res.verdict == Verdict.PASS
    assert res.levers_fired == []


def test_gate_fires_levers_in_order():
    slo = config.load_slo()
    # Choose a PG knee that fails at baseline but passes after some levers.
    p = _params()
    baseline_projected = projected_db_load_rps(8000, p) * slo.capacity_gate.headroom_factor
    # pg just under baseline so at least one lever must fire
    pg = baseline_projected - 1
    res = evaluate_gate(
        pg_knee_rps=pg, projected_knee_rps=8000, cfg=slo.capacity_gate, params=_params()
    )
    # Either it passed after firing >=1 lever, or it exhausted them honestly.
    if res.verdict == Verdict.PASS:
        assert len(res.levers_fired) >= 1
        # first lever fired is write_share (spec order)
        assert res.levers_fired[0].name == "write_share"
    else:
        assert res.verdict == Verdict.DB_IMPOSED_SHARED_KNEE


def test_gate_honest_fallback_when_levers_exhausted():
    slo = config.load_slo()
    res = evaluate_gate(
        pg_knee_rps=1.0, projected_knee_rps=100_000,
        cfg=slo.capacity_gate, params=_params(),
    )
    assert res.verdict == Verdict.DB_IMPOSED_SHARED_KNEE
    # all three levers attempted (write_share, cache_hit_rate, pg_cores)
    assert {lv.name for lv in res.levers_fired} == {"write_share", "cache_hit_rate", "pg_cores"}


def test_lever_machine_never_synchronous_commit_off():
    slo = config.load_slo()
    machine = LeverMachine(slo.capacity_gate, _params())
    fired = []
    while (lev := machine.try_next()) is not None:
        fired.append(lev.name)
    assert "synchronous_commit_off" not in fired
    assert fired == ["write_share", "cache_hit_rate", "pg_cores"]


def test_resolved_params_from_workload():
    wl = config.load_workload()
    p = ResolvedWorkloadParams.from_workload(wl)
    assert p.write_share == wl.mix.create_invoice / 100.0  # 0.15
    assert p.cache_hit_rate == 0.80
    assert p.pg_cores == 3


# --------------------------------------------------------------------------- #
# F1/3: a "knee" needs a LADDER, not one rung
# --------------------------------------------------------------------------- #
def _ladder(n: int, *, start: int = 500, step: float = 1.2) -> list[RungResult]:
    """n rungs that all track their offered rate cleanly."""
    out = []
    rate = float(start)
    for _ in range(n):
        out.append(RungResult(rate=int(rate), achieved=rate, p99_ms=3.0, error_rate=0.0))
        rate *= step
    return out


def test_single_rung_is_not_a_knee():
    # The 2026-06-06 run: the create-only ladder broke on its FIRST rung and the
    # first rung's achieved rate was published as "the measured PG ceiling".
    knee = pg_knee_from_rungs(_ladder(1))
    assert knee.measured is False
    assert knee.knee_rps is None
    assert knee.usable_rungs == 1
    assert "no usable PG knee measured" in knee.detail


def test_knee_needs_min_usable_rungs():
    for n in range(MIN_PG_KNEE_RUNGS):
        assert pg_knee_from_rungs(_ladder(n)).measured is False
    assert pg_knee_from_rungs(_ladder(MIN_PG_KNEE_RUNGS)).measured is True


def test_unusable_rungs_do_not_count_toward_the_minimum():
    rungs = _ladder(2)
    # two rungs that did NOT track offered (generator fell behind) + one errored
    rungs.append(RungResult(rate=900, achieved=500.0, p99_ms=90.0, error_rate=0.0))
    rungs.append(RungResult(rate=1000, achieved=1000.0, p99_ms=90.0, error_rate=0.5))
    knee = pg_knee_from_rungs(rungs)
    assert knee.usable_rungs == 2
    assert knee.measured is False


def test_knee_is_top_usable_rung_and_flags_lower_bound():
    rungs = _ladder(4)
    broken = RungResult(rate=1100, achieved=700.0, p99_ms=200.0, error_rate=0.0)
    knee = pg_knee_from_rungs([*rungs, broken])
    assert knee.measured and knee.knee_rps == pytest.approx(rungs[-1].achieved)
    assert knee.lower_bound is False
    # a ladder that never broke: the knee is only a FLOOR, flagged as such
    capped = pg_knee_from_rungs(_ladder(5))
    assert capped.lower_bound is True and "LOWER BOUND" in capped.detail


def test_unmeasured_knee_result_is_its_own_verdict():
    slo = config.load_slo()
    res = unmeasured_knee_result(pg_knee_from_rungs(_ladder(1)), cfg=slo.capacity_gate)
    assert res.verdict == Verdict.NO_PG_KNEE_MEASURED
    assert res.pg_knee_measured is False
    assert res.pg_knee_rps is None          # never a fallback number
    assert res.passed is False


# --------------------------------------------------------------------------- #
# F1: fired levers must be REALIZABLE, and only realized levers may change the
# effective workload
# --------------------------------------------------------------------------- #
def test_write_share_lever_rewrites_the_mix_and_keeps_it_valid():
    wl = config.load_workload()
    p = ResolvedWorkloadParams.from_workload(wl)
    p.write_share = 0.10
    eff = apply_params_to_workload(wl, p)
    assert wl.mix.create_invoice == 15          # spec untouched
    assert eff.mix.create_invoice == 10
    # the freed share lands on the one endpoint that never touches PG, so the
    # gate's DB-load projection stays exactly true after the rewrite
    assert eff.mix.pricing_quote == wl.mix.pricing_quote + 5
    assert sum(w for _, w in eff.mix.as_pairs()) == 100


def test_cache_hit_rate_lever_rewrites_the_target():
    wl = config.load_workload()
    p = ResolvedWorkloadParams.from_workload(wl)
    p.cache_hit_rate = 0.90
    eff = apply_params_to_workload(wl, p)
    assert wl.cache_construction.target_hit_rate == 0.80
    assert eff.cache_construction.target_hit_rate == 0.90


def test_non_integral_write_share_is_refused():
    wl = config.load_workload()
    p = ResolvedWorkloadParams.from_workload(wl)
    p.write_share = 0.125
    with pytest.raises(ValueError, match="integer mix percentage"):
        apply_params_to_workload(wl, p)


def test_pg_cores_lever_moves_cores_off_the_donor():
    env = pg_cpuset_overrides(4, taken_from="vegeta")
    assert env == {"PG_CPUS": "8-11", "VEGETA_CPUS": "12-13"}
    # no-op when PG already has the cores
    assert pg_cpuset_overrides(3, taken_from="vegeta") == {}
    # a donor that cannot spare a core fails loudly (never silently skipped)
    with pytest.raises(ValueError, match="cannot spare"):
        pg_cpuset_overrides(6, taken_from="vegeta", donor_cpus="11-13")
    with pytest.raises(ValueError, match="taken_from"):
        pg_cpuset_overrides(4, taken_from=None)


def test_cpuset_parse_format_roundtrip():
    assert parse_cpuset("8-10,14") == [8, 9, 10, 14]
    assert format_cpuset([8, 9, 10, 14]) == "8-10,14"
    assert format_cpuset([5]) == "5"


def test_cache_hit_rate_lever_is_not_applied_without_a_ttl():
    levers = [LeverApplication(name="cache_hit_rate", from_value=0.8, to_value=0.9)]
    rz = realize_levers(levers, cache_ttl_seconds=None)
    assert rz.applied == []
    assert "cache_hit_rate" in rz.not_applied
    assert rz.fully_applied is False
    # with an operator-supplied TTL it becomes a real compose override
    rz2 = realize_levers(levers, cache_ttl_seconds=900)
    assert rz2.applied == ["cache_hit_rate"]
    assert rz2.env_overrides["CACHE_TTL_SECONDS"] == "900"
    assert rz2.fully_applied is True


def test_only_applied_levers_reach_the_effective_params():
    wl = config.load_workload()
    spec = ResolvedWorkloadParams.from_workload(wl)
    fired = ResolvedWorkloadParams.from_workload(wl)
    fired.write_share, fired.cache_hit_rate, fired.pg_cores = 0.10, 0.90, 4
    # cache_hit_rate fired but could not be applied -> it must NOT show up
    out = params_for_applied_levers(spec, fired, ["write_share", "pg_cores"])
    assert out.write_share == 0.10
    assert out.pg_cores == 4
    assert out.cache_hit_rate == spec.cache_hit_rate


def test_effective_workload_record_shows_spec_vs_effective():
    wl = config.load_workload()
    p = ResolvedWorkloadParams.from_workload(wl)
    p.write_share, p.pg_cores = 0.10, 4
    eff = apply_params_to_workload(wl, p)
    levers = [
        LeverApplication(name="write_share", from_value=0.15, to_value=0.10),
        LeverApplication(name="pg_cores", from_value=3, to_value=4, taken_from="vegeta"),
    ]
    rz = realize_levers(levers, cache_ttl_seconds=None)
    rec = effective_workload_record(wl, eff, p, realization=rz, levers_fired=levers)
    assert rec.spec_write_share == 0.15 and rec.effective_write_share == 0.10
    assert rec.spec_pg_cores == 3 and rec.effective_pg_cores == 4
    assert rec.spec_mix["create_invoice"] == 15
    assert rec.effective_mix["create_invoice"] == 10
    assert rec.write_share_absorbed_by == "pricing_quote"
    assert rec.differs_from_spec is True
    assert rec.env_overrides["PG_CPUS"] == "8-11"


def test_headroom_after_remeasure_does_not_rescale():
    slo = config.load_slo()
    p = ResolvedWorkloadParams.from_workload(config.load_workload())
    p.pg_cores = 4
    projected = projected_db_load_rps(8000, p)
    need = projected * slo.capacity_gate.headroom_factor
    ok, got = headroom_after_remeasure(
        remeasured_pg_knee_rps=need, projected_knee_rps=8000,
        cfg=slo.capacity_gate, params=p,
    )
    assert ok and got == pytest.approx(projected)
    # the 4/3 linear scaling must NOT be applied to a re-measured 4-core knee
    ok2, _ = headroom_after_remeasure(
        remeasured_pg_knee_rps=need * 3 / 4, projected_knee_rps=8000,
        cfg=slo.capacity_gate, params=p,
    )
    assert ok2 is False


# --------------------------------------------------------------------------- #
# create-only targets are ABSOLUTE URLs (the 2026-08-14 gate root cause)
# --------------------------------------------------------------------------- #
def test_create_only_targets_emit_absolute_urls(tmp_path):
    """vegeta's attack argv has no base-URL flag: a schemeless `POST /invoices`
    fails per-request at ~10 us without touching the SUT. That was the gate's
    state from 2026-06-06 (the one-rung "PG ceiling" it reported was pure
    vegeta errors) until the no-usable-rung refusal caught it on 2026-08-14. Every
    emitted target line must carry the SUT's absolute compose-network URL."""
    from ledgerbench.capacity_gate import create_only_targets
    from ledgerbench.config import load_workload

    wl = load_workload()
    create_only_targets(wl, out_dir=tmp_path, count=50, seed=42, scale="smoke")
    lines = (tmp_path / "targets.txt").read_text(encoding="utf-8").splitlines()
    posts = [ln for ln in lines if ln.startswith("POST ")]
    assert posts, "no POST targets emitted"
    for ln in posts:
        assert ln == "POST http://sut-go:8080/invoices", ln
