"""LiveProbeRunner logic that is testable without a docker daemon.

The attack/compose paths belong to the measured run; what is unit-tested here is
the state the runner carries INTO those calls: the realized capacity-gate levers
(compose env on every invocation, effective workload for targets generation) and
the window-boundary sampling helpers whose null-vs-zero distinction the manifest
depends on.
"""

from __future__ import annotations

import pytest

from ledgerbench import compose
from ledgerbench.config import load_slo, load_workload
from ledgerbench.live_runner import (
    LiveProbeRunner,
    _cache_delta,
    _fault_delta,
)
from ledgerbench.phases import ScaleProfile


@pytest.fixture
def runner():
    slo = load_slo()
    return LiveProbeRunner(
        run_id="lr-1", profile=ScaleProfile.resolve("smoke", slo),
        workload=load_workload(), slo=slo, bench_overlay=True,
    )


def test_lever_env_reaches_every_compose_invocation(runner):
    assert runner._env().extra_env == {}
    runner.apply_effective(env_overrides={"PG_CPUS": "8-11", "VEGETA_CPUS": "12-13"})
    for profiles in ([], [compose.PROFILE_LOAD], [compose.PROFILE_GO]):
        env = runner._env(profiles=profiles)
        assert env.extra_env["PG_CPUS"] == "8-11"
        assert env.extra_env["VEGETA_CPUS"] == "12-13"


def test_apply_effective_swaps_the_workload_and_forces_regeneration(runner):
    from ledgerbench import capacity_gate as capgate

    params = capgate.ResolvedWorkloadParams.from_workload(runner.workload)
    params.write_share = 0.10
    effective = capgate.apply_params_to_workload(runner.workload, params)
    runner._shipped.add(("go", "headline.mixed.gc-default"))
    runner.apply_effective(workload=effective)
    assert runner.workload.mix.create_invoice == 10
    # the shipped set is cleared so the stale (pre-lever) targets are re-made
    assert runner._shipped == set()


def test_fault_delta_is_null_when_a_boundary_read_failed():
    assert _fault_delta({"major": 5, "minor": 9}, {"major": 12, "minor": 20}) == {
        "major_faults_delta": 7, "minor_faults_delta": 11}
    # a missing read must NOT become a zero fault count
    out = _fault_delta({"major": None, "minor": None}, {"major": 12, "minor": 20})
    assert out == {"major_faults_delta": None, "minor_faults_delta": None}


def test_cache_delta_reports_measured_hit_rate_or_says_not_measured():
    out = _cache_delta(
        {"keyspace_hits": 100, "keyspace_misses": 100},
        {"keyspace_hits": 900, "keyspace_misses": 300},
    )
    assert out["keyspace_hits"] == 800 and out["keyspace_misses"] == 200
    assert out["hit_rate"] == pytest.approx(0.8)
    assert out["source"] != "not_measured"

    missing = _cache_delta({"keyspace_hits": None, "keyspace_misses": None}, {})
    assert missing["hit_rate"] is None
    assert missing["source"] == "not_measured"

    idle = _cache_delta(
        {"keyspace_hits": 1, "keyspace_misses": 1}, {"keyspace_hits": 1, "keyspace_misses": 1}
    )
    assert idle["hit_rate"] is None          # 0 ops != a 0% hit rate
    assert idle["keyspace_hits"] == 0


def test_measure_pg_create_knee_returns_a_measurement_not_a_float(runner, monkeypatch, tmp_path):
    """A ladder that cannot even start reports NOT MEASURED, never a fallback rate."""
    monkeypatch.setattr(
        compose, "compose",
        lambda *a, **k: type("R", (), {"ok": False, "stdout": "", "stderr": "no daemon",
                                       "returncode": 1})(),
    )
    knee = runner.measure_pg_create_knee(out_dir=tmp_path, rungs=3, window_s=1)
    assert knee.measured is False
    assert knee.knee_rps is None
    assert "no usable PG knee measured" in knee.detail


def test_every_knee_path_persists_pg_knee_json(runner, monkeypatch, tmp_path):
    """Even the "compose up failed" exit leaves the artifact behind.

    The early returns used to skip the write entirely, so the run had no record
    that the create-only ladder was attempted at all.
    """
    import json

    monkeypatch.setattr(
        compose, "compose",
        lambda *a, **k: type("R", (), {"ok": False, "stdout": "", "stderr": "no daemon",
                                       "returncode": 1})(),
    )
    out_dir = tmp_path / "capacity-gate"
    knee = runner.measure_pg_create_knee(out_dir=out_dir, rungs=3, window_s=1)
    doc = json.loads((out_dir / "pg_knee.json").read_text(encoding="utf-8"))
    assert doc["knee_rps"] is None
    assert doc["rungs"] == []
    assert doc["usable_rungs"] == 0
    assert doc["measured_at_cores"] == 3
    assert doc["detail"] == knee.detail
    assert "failed to come up" in doc["detail"]
