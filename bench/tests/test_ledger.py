"""Resumable run-ledger: schema round-trip + probe-granular resume."""

from __future__ import annotations

import pytest

from ledgerbench import ledger as ledmod
from ledgerbench.ledger import Ledger, ProbeRecord, State
from ledgerbench.util import paths


@pytest.fixture
def isolated_results(tmp_path, monkeypatch):
    """Point results_dir at a tmp dir so tests never touch the repo's results/."""
    monkeypatch.setattr(paths, "results_dir", lambda: tmp_path / "results")
    return tmp_path / "results"


def test_create_save_load_roundtrip(isolated_results):
    led = Ledger.load_or_create("run-1", config_hash="abc", rng_seed=42)
    led.mark_phase("doctor", State.DONE, artifact="doctor.json")
    again = Ledger.load("run-1")
    assert again is not None
    assert again.run_id == "run-1"
    assert again.config_hash == "abc"
    assert again.phase_done("doctor")
    assert again.phases["doctor"].data["artifact"] == "doctor.json"


def test_config_hash_mismatch_refuses_resume(isolated_results):
    Ledger.load_or_create("run-2", config_hash="hash-A", rng_seed=42)
    with pytest.raises(RuntimeError, match="config_hash changed"):
        Ledger.load_or_create("run-2", config_hash="hash-B", rng_seed=42)


def test_probe_granular_resume(isolated_results):
    led = Ledger.load_or_create("run-3", config_hash="c", rng_seed=7)
    cell_id = "headline.mixed.gc-default"
    stage = led.stage(cell_id, "confirm")
    for block in range(3):
        for lang in ("go", "dotnet"):
            stage.probes.append(
                ProbeRecord(block=block, slot_order=["go", "dotnet"], lang=lang, rate=8120)
            )
    # mark the first two probes done
    led.mark_probe_done(
        cell_id, "confirm", stage.probes[0], artifact="b0.go.bin", achieved=8118.0, error_rate=0.0
    )
    led.mark_probe_done(
        cell_id, "confirm", stage.probes[1], artifact="b0.dotnet.bin", achieved=8121.0, error_rate=0.0
    )
    # reload and confirm only the remaining 4 are pending
    reloaded = Ledger.load("run-3")
    pending = reloaded.pending_probes(cell_id, "confirm")
    assert len(pending) == 4
    assert all(p.state != State.DONE for p in pending)
    done = [p for p in reloaded.stage(cell_id, "confirm").probes if p.state == State.DONE]
    assert {p.artifact for p in done} == {"b0.go.bin", "b0.dotnet.bin"}


def test_atomic_write_leaves_no_tmp(isolated_results):
    led = Ledger.load_or_create("run-4", config_hash="c", rng_seed=1)
    led.save()
    run_path = paths.run_dir("run-4")
    leftovers = list(run_path.glob(".ledger.json.*"))
    assert leftovers == []
    assert led.path().exists()


def test_schema_version_present(isolated_results):
    led = Ledger.load_or_create("run-5", config_hash="c", rng_seed=1)
    assert led.schema_version == ledmod.LEDGER_SCHEMA_VERSION


def test_latest_and_latest_unfinished(isolated_results):
    old = Ledger.load_or_create("r-old", config_hash="c", rng_seed=1)
    old.created_utc = "2026-01-01T00:00:00.000000Z"
    old.mark_phase("doctor", State.DONE)
    old.save()
    new = Ledger.load_or_create("r-new", config_hash="c", rng_seed=1)
    new.created_utc = "2026-12-31T00:00:00.000000Z"
    new.save()
    assert Ledger.latest().run_id == "r-new"
    # neither is complete (no host_restore) -> latest_unfinished is the newest
    assert Ledger.latest_unfinished().run_id == "r-new"


def test_is_complete_requires_all_phases_and_stages(isolated_results):
    led = Ledger.load_or_create("r-c", config_hash="c", rng_seed=1)
    assert not led.is_complete()  # empty
    for ph in ("doctor", "host_setup", "seed", "capacity_gate", "validation",
               "cells", "variants", "host_restore"):
        led.mark_phase(ph, State.DONE)
    cid = "headline.mixed.gc-default"
    led.mark_stage(cid, "ladder", State.DONE)
    led.mark_stage(cid, "fit", State.DONE)
    led.mark_stage(cid, "confirm", State.DONE)
    assert not led.is_complete()  # soak still pending
    led.mark_stage(cid, "soak", State.DONE)
    assert led.is_complete()


def test_next_pending_walks_phase_order(isolated_results):
    led = Ledger.load_or_create("r-np", config_hash="c", rng_seed=1)
    assert led.next_pending() == "doctor"
    led.mark_phase("doctor", State.DONE)
    led.mark_phase("host_setup", State.DONE)
    assert led.next_pending() == "seed"
