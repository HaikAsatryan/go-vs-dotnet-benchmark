"""Config loading + validation against the real spec files."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ledgerbench import config
from ledgerbench.config import Mix, PageDistribution


def test_workload_loads(workload):
    assert workload.zipf.alpha == 1.0
    assert workload.rng.algorithm == "PCG64"
    assert workload.rng.target_generator_seed_default == 12345
    assert workload.seeding.seed == 42
    assert workload.seeding.scales["smoke"].customers == 1000
    assert workload.seeding.scales["full"].invoices == 5_000_000


def test_slo_loads(slo):
    assert slo.slo.threshold_ms == 20
    assert slo.slo.error_rate_max == 0.001
    assert slo.equivalence.margin_pct == 5.0
    assert slo.ladder.confirm_reps == 10
    assert slo.ladder.step_factor == 1.2
    assert slo.capacity_gate.headroom_factor == 1.3
    names = [lv.name for lv in slo.capacity_gate.levers_in_order]
    assert names == ["write_share", "cache_hit_rate", "pg_cores"]


def test_lever_from_to_alias(slo):
    write = slo.capacity_gate.levers_in_order[0]
    assert write.from_ == 0.15
    assert write.to == 0.10
    cores = slo.capacity_gate.levers_in_order[2]
    assert cores.taken_from == "vegeta"


def test_mix_must_sum_100():
    with pytest.raises(ValidationError):
        Mix(get_invoice=10, list_invoices=10, pricing_quote=10,
            create_invoice=10, pdf_stub=10, healthz=10)  # sums to 60


def test_page_weights_must_sum_1():
    with pytest.raises(ValidationError):
        PageDistribution(weights={1: 0.5, 2: 0.2})  # 0.7


def test_config_hash_stable_and_changes(tmp_path):
    h1 = config.config_hash()
    h2 = config.config_hash()
    assert h1 == h2 and len(h1) == 64

    # config_hash covers the WHOLE spec tree, not just the two YAMLs. Copy the
    # real spec/ into a temp dir, hash it (must match), then mutate ANY file
    # (here a non-YAML contract) and confirm the hash moves.
    import shutil
    from ledgerbench.util import paths
    spec_copy = tmp_path / "spec"
    shutil.copytree(paths.spec_dir(), spec_copy)
    assert config.config_hash(spec_dir=spec_copy) == h1

    contract = spec_copy / "schema.sql"
    contract.write_text(contract.read_text(encoding="utf-8") + "\n-- edit\n", encoding="utf-8")
    assert config.config_hash(spec_dir=spec_copy) != h1


def test_config_hash_sees_a_new_spec_file(tmp_path):
    """A file added anywhere under spec/ changes the hash (full-tree coverage)."""
    import shutil
    from ledgerbench.util import paths
    spec_copy = tmp_path / "spec"
    shutil.copytree(paths.spec_dir(), spec_copy)
    base = config.config_hash(spec_dir=spec_copy)
    (spec_copy / "postgres" / "extra.conf").write_text("x=1\n", encoding="utf-8")
    assert config.config_hash(spec_dir=spec_copy) != base
