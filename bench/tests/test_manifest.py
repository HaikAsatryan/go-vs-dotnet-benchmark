"""RunManifest full-disclosure schema round-trip."""

from __future__ import annotations

import json

import pytest

from ledgerbench import manifest as mf
from ledgerbench.manifest import (
    AchievedVsOffered,
    FaultCounts,
    Knobs,
    ResourceDelta,
    RunManifest,
    ThrottleCounters,
    new_manifest,
)
from ledgerbench.util import paths


@pytest.fixture
def isolated_results(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "results_dir", lambda: tmp_path / "results")
    return tmp_path / "results"


def test_empty_manifest_roundtrips():
    m = new_manifest("run-x", config_hash="deadbeef")
    s = m.model_dump_json()
    again = RunManifest.model_validate_json(s)
    assert again.identity.run_id == "run-x"
    assert again.identity.config_hash == "deadbeef"
    assert again.schema_version == mf.MANIFEST_SCHEMA_VERSION


def test_full_disclosure_sections_present():
    m = new_manifest("run-y")
    # every full-disclosure section is a field on the model
    for field in (
        "identity", "versions", "knobs_go", "knobs_dotnet", "host_state",
        "topology", "postgres_config", "achieved_vs_offered", "throttle_counters",
        "fault_counts", "resource_deltas", "cache_stats", "errors",
    ):
        assert hasattr(m, field)


def test_knobs_echo_every_r10_lever():
    k = Knobs(
        db_pool_max=24, db_pool_min=24, db_conn_lifetime_seconds=3600,
        redis_pool_max=16, cache_ttl_seconds=300, log_queue_len=8192,
        gogc="100", gomemlimit="9663676416", gc_dynamic_adaptation_mode="1",
        max_auto_prepare=0, data_layer="sqlc", gomaxprocs=6, processor_count=6,
    )
    d = json.loads(k.model_dump_json())
    # asserted parity values: GOMAXPROCS and DOTNET_PROCESSOR_COUNT both pinned
    # to the SUT core count
    assert d["gomaxprocs"] == 6
    assert d["processor_count"] == 6
    assert d["data_layer"] == "sqlc"


def test_populated_manifest_roundtrips_and_saves(isolated_results):
    m = new_manifest("run-z", config_hash="c0ffee")
    m.knobs_go = Knobs(gomaxprocs=6, gogc="100", db_pool_max=24)
    m.knobs_dotnet = Knobs(processor_count=6, max_auto_prepare=0, db_pool_max=24)
    m.achieved_vs_offered.append(
        AchievedVsOffered(probe_id="b0.go", offered_rate=8000, achieved_rate=7990, ratio=0.99875)
    )
    m.throttle_counters.append(ThrottleCounters(container="ledgerline-sut-go-1"))
    m.fault_counts.append(FaultCounts(container="ledgerline-sut-go-1", major_faults=0, minor_faults=123))
    m.resource_deltas.append(ResourceDelta(container="ledgerline-sut-go-1", cpu_usage_usec_delta=42_000))
    m.versions.image_digests["postgres"] = "postgres:18.4-bookworm"  # tag now, digest at smoke
    m.save()

    loaded = RunManifest.load("run-z")
    assert loaded is not None
    assert loaded.knobs_go.gomaxprocs == 6
    assert loaded.achieved_vs_offered[0].ratio == pytest.approx(0.99875)
    assert loaded.throttle_counters[0].nr_throttled == 0
    assert loaded.versions.image_digests["postgres"].startswith("postgres:18.4")


def test_extra_fields_rejected_on_strict_sections():
    # Identity forbids extras; a typo'd key must fail validation (full-disclosure
    # schema integrity).
    with pytest.raises(Exception):
        mf.Identity.model_validate({"run_id": "r", "bogus_key": 1})


# --------------------------------------------------------------------------- #
# F8: per-cell knobs, explicit "not measured", working-set disclosure
# --------------------------------------------------------------------------- #
def test_per_cell_knobs_roundtrip(isolated_results):
    m = new_manifest("run-cells")
    m.knobs_by_cell["headline.mixed.gc-default"] = mf.CellKnobs(
        cell_id="headline.mixed.gc-default", go=Knobs(gomaxprocs=6))
    m.knobs_by_cell["headline.mixed.gc-generous"] = mf.CellKnobs(
        cell_id="headline.mixed.gc-generous",
        extra_env={"GOGC": "off"},
        go=Knobs(gogc="off", gomemlimit="10GiB", gomaxprocs=6),
        dotnet=Knobs(gc_dynamic_adaptation_mode="0"),
    )
    m.save()
    loaded = RunManifest.load("run-cells")
    assert loaded.schema_version == 2
    # the defaults cell is NOT overwritten by the generous cell's knobs
    assert loaded.knobs_by_cell["headline.mixed.gc-default"].go.gogc is None
    assert loaded.knobs_by_cell["headline.mixed.gc-generous"].go.gogc == "off"
    # no cross-copy: the .NET row of the generous cell has no Go knobs
    generous_dotnet = loaded.knobs_by_cell["headline.mixed.gc-generous"].dotnet
    assert generous_dotnet.gogc is None and generous_dotnet.gomemlimit is None


def test_unmeasured_sections_default_to_not_measured():
    m = new_manifest("run-nm")
    assert m.cache_stats.source == mf.NOT_MEASURED
    assert m.cache_stats.keyspace_hits is None
    # a fault row with no data carries None, never a 0 that reads as measured
    row = mf.FaultCounts(container="c")
    assert row.major_faults is None and row.source == mf.NOT_MEASURED
    assert m.errors.source == mf.NOT_MEASURED


def test_capacity_gate_and_effective_workload_sections_roundtrip(isolated_results):
    m = new_manifest("run-gate")
    m.capacity_gate = mf.CapacityGateStatus(
        verdict="db-imposed-shared-knee", degraded=True,
        degraded_allowed_by="LEDGERBENCH_ALLOW_DB_IMPOSED_KNEE",
        levers_fired=["write_share"], levers_not_applied={"cache_hit_rate": "no TTL"},
    )
    m.effective_workload = mf.EffectiveWorkload(
        spec_mix={"create_invoice": 15}, effective_mix={"create_invoice": 10},
        spec_pg_cores=3, effective_pg_cores=4,
    )
    m.save()
    loaded = RunManifest.load("run-gate")
    assert loaded.capacity_gate.degraded is True
    assert loaded.effective_workload.differs_from_spec is True


def test_working_set_from_targets(tmp_path):
    from ledgerbench import config, vegeta

    wl = config.load_workload()
    vegeta.generate_targets(wl, out_dir=tmp_path, count=300,
                            seed=wl.rng.target_generator_seed_default, scale="smoke")
    ws = mf.working_set_from_targets(tmp_path / "targets.txt")
    assert ws.target_count == 300
    # a 300-request replay cannot touch more than 300 distinct ids of the 5000
    # seeded at smoke scale -- which is the entire point of recording it
    assert 0 < ws.distinct_invoice_ids <= 300
    assert 0 < ws.distinct_customer_ids <= 300
    assert "distinct ids counted over 300 generated REQUESTS" in ws.note


def test_working_set_customer_components_are_reported_separately(tmp_path):
    """distinct_customer_ids is a UNION; both halves are published verbatim.

    The audit quotes the list-URL count and the manifest quotes the union, so
    the two numbers disagree unless the components are visible.
    """
    from ledgerbench import config, vegeta

    wl = config.load_workload()
    vegeta.generate_targets(wl, out_dir=tmp_path, count=400,
                            seed=wl.rng.target_generator_seed_default, scale="smoke")
    ws = mf.working_set_from_targets(tmp_path / "targets.txt")
    assert ws.distinct_customer_ids_list_urls > 0
    assert ws.distinct_customer_ids_create_bodies > 0
    # a union is at least as big as either part and at most their sum
    assert ws.distinct_customer_ids >= ws.distinct_customer_ids_list_urls
    assert ws.distinct_customer_ids >= ws.distinct_customer_ids_create_bodies
    assert ws.distinct_customer_ids <= (
        ws.distinct_customer_ids_list_urls + ws.distinct_customer_ids_create_bodies
    )
    assert "UNION" in ws.note


def test_working_set_note_does_not_claim_to_bound_the_write_path(tmp_path):
    from ledgerbench import config, vegeta

    wl = config.load_workload()
    vegeta.generate_targets(wl, out_dir=tmp_path, count=100,
                            seed=wl.rng.target_generator_seed_default, scale="smoke")
    ws = mf.working_set_from_targets(tmp_path / "targets.txt")
    assert "ONLY keys the measured traffic touches" not in ws.note
    assert "write path" in ws.note and "READ path" in ws.note
    # target_count is REQUESTS; the file itself has more lines than that
    lines = [ln for ln in (tmp_path / "targets.txt").read_text().splitlines() if ln.strip()]
    assert len(lines) > ws.target_count
    assert "REQUESTS" in ws.note


def test_unpopulated_sections_carry_an_explicit_not_measured_marker():
    """host_state / topology / postgres_config are written by NO code.

    An empty object with no marker reads as "measured and empty"; each of the
    three says `not_measured` and names what would have filled it.
    """
    m = new_manifest("run-unpop")
    for section in (m.host_state, m.topology, m.postgres_config):
        assert section.source == mf.NOT_MEASURED
        assert "not populated by the runner" in section.note
    assert m.topology.containers == {}
    assert m.postgres_config.values == {}


def test_aggregate_provenance_marks_every_list_section():
    """An empty LIST has no row to carry a `source`; the section does."""
    m = new_manifest("run-prov")
    prov = m.aggregate_provenance
    assert prov.throttle_counters == mf.NOT_MEASURED
    assert prov.resource_deltas == mf.NOT_MEASURED
    assert prov.fault_counts == mf.NOT_MEASURED
    assert prov.achieved_vs_offered == mf.NOT_MEASURED
    # and the row type that had no marker at all now has one
    assert ThrottleCounters(container="c").source == mf.NOT_MEASURED


def test_throttle_counters_document_lifetime_semantics():
    doc = ThrottleCounters.__doc__
    assert "CUMULATIVE" in doc and "lifetime" in doc
    assert "not \"in the measured window\"" in doc


def test_capacity_gate_status_keeps_measured_and_projected_apart():
    """F1: a linearly-scaled projection may not sit in a field read as measured."""
    st = mf.CapacityGateStatus(
        verdict="pass", pg_knee_rps=500.0057, pg_knee_measured=True,
        pg_knee_measured_at_cores=3, pg_capacity_projected_rps=666.674,
        pg_capacity_projection_basis="measured 500.0057 rps at 3 cores x 4/3",
    )
    assert st.pg_knee_rps != st.pg_capacity_projected_rps
    assert "MEASURED" in mf.CapacityGateStatus.__doc__


def test_degraded_docstring_matches_the_code_that_sets_it():
    """The flag is set on ANY non-pass verdict, aborted runs included."""
    doc = mf.CapacityGateStatus.__doc__
    assert "did NOT return `pass`" in doc
    assert "degraded_allowed_by" in doc


def test_identity_records_a_resume_from_different_code(isolated_results):
    m = new_manifest("run-sha")
    m.identity.git_sha = "aaa"
    m.identity.git_sha_changed_during_run.append(
        {"at": "2026-08-13T00:00:00.000000Z", "git_sha": "bbb", "git_dirty": False}
    )
    m.save()
    loaded = RunManifest.load("run-sha")
    assert loaded.identity.git_sha == "aaa"          # the sha the run STARTED at
    assert loaded.identity.git_sha_changed_during_run[0]["git_sha"] == "bbb"
