"""Phase orchestration + RCB resume logic, docker-free.

These tests drive the cell stage functions (ladder/fit/confirm/soak) and the
ledger resume helpers with a FAKE ProbeRunner, so the orchestration logic is
exercised end-to-end with no daemon. The live docker paths (seed, host-setup,
equivalence) are covered by the kill-resume drill, not here.
"""

from __future__ import annotations

import pytest

from ledgerbench import capacity_gate as capgate
from ledgerbench import phases
from ledgerbench.config import load_slo, load_workload
from ledgerbench.ledger import Ledger, ProbeRecord, State
from ledgerbench.manifest import new_manifest
from ledgerbench.phases import ProbeOutcome, RunContext, ScaleProfile
from ledgerbench.util import paths


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def isolated_results(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "results_dir", lambda: tmp_path / "results")
    return tmp_path / "results"


class FakeRunner:
    """Deterministic, no-docker probe runner.

    `crossing` controls whether the synthetic per-second p99 series crosses the
    SLO: when False (smoke default) the series sits at ~5 ms (sub-knee) so the
    ladder never brackets a crossing; the no-crossing path. When True, p99 grows
    with the rate so a crossing is bracketed.
    """

    def __init__(self, *, crossing: bool = False, fail_on=None, under_warm_on=None):
        self.calls = 0
        self.crossing = crossing
        self.fail_on = fail_on or set()        # set of (lang, rate) to fail
        # set of (lang, rate) to flag under-warmed (excluded from the fit)
        self.under_warm_on = under_warm_on or set()
        self.probe_log: list[tuple[str, str, int, int]] = []

    def ensure_targets(self, lang, cell):  # noqa: D401
        pass

    def run_probe(self, *, cell, stage, lang, rate, block, duration_s, warmup_cap_s, artifact_dir):
        self.calls += 1
        self.probe_log.append((stage, lang, rate, block))
        if (lang, rate) in self.fail_on:
            return ProbeOutcome(achieved=0, error_rate=1.0, p99_ms=0, artifact="",
                                ok=False, detail="injected failure")
        if self.crossing:
            base = 4.0 + (rate / 100.0)        # grows with rate -> eventually >20
        else:
            base = 5.0
        # The per-second series sits well BELOW the pooled p99 so a test that
        # consumed the series instead of p99_ms would mis-bracket: this guards the
        # FIX-1 "pooled p99 drives the fit, not percentile-of-percentiles" contract.
        series = [1.0 + (i % 3) * 0.05 for i in range(10)]
        return ProbeOutcome(
            achieved=rate * 0.999, error_rate=0.0, p99_ms=base,
            artifact=f"cells/{cell.cell_id}/{stage}/{lang}.json",
            per_second_p99=series, under_warmed=(lang, rate) in self.under_warm_on,
            ok=True,
        )


def _ctx(run_id, scale="smoke", runner=None) -> RunContext:
    wl = load_workload()
    slo = load_slo()
    profile = ScaleProfile.resolve(scale, slo)
    led = Ledger.load_or_create(run_id, config_hash="testhash", rng_seed=wl.seeding.seed)
    return RunContext(
        led=led, profile=profile, workload=wl, slo=slo,
        manifest=new_manifest(run_id), runner=runner or FakeRunner(), run_id=run_id,
    )


_GC_DEFAULT = next(c for c in phases.CELLS if c.cell_id == "headline.mixed.gc-default")


# --------------------------------------------------------------------------- #
# smoke-params resolution
# --------------------------------------------------------------------------- #
def test_smoke_profile_is_functional_rehearsal():
    p = ScaleProfile.resolve("smoke", load_slo())
    assert p.functional_only is True
    assert p.warmup_cap_s == 15
    assert p.measure_s == 10
    assert p.ladder_start_rps == 100
    assert p.ladder_step_factor == 2.0
    assert p.ladder_max_rungs == 3
    assert p.ladder_reps_per_rung == 2
    assert p.confirm_blocks == 2
    assert p.soak_s == 20
    assert p.cells_filter == {"headline.mixed.gc-default"}


def test_full_profile_reads_slo_verbatim():
    slo = load_slo()
    p = ScaleProfile.resolve("full", slo)
    assert p.functional_only is False
    assert p.warmup_cap_s == slo.warmup.hard_cap_seconds          # 180
    assert p.ladder_start_rps == slo.ladder.start_rate_rps_default  # 500
    assert p.ladder_step_factor == slo.ladder.step_factor          # 1.2
    assert p.ladder_reps_per_rung == slo.ladder.reps_per_rung      # 3
    assert p.confirm_blocks == slo.ladder.confirm_reps             # 10
    assert p.cells_filter is None


def test_smoke_runs_only_gc_default_cell():
    p = ScaleProfile.resolve("smoke", load_slo())
    ids = [c.cell_id for c in phases.cells_for(p)]
    assert ids == ["headline.mixed.gc-default"]


def test_full_runs_both_headline_cells():
    p = ScaleProfile.resolve("full", load_slo())
    ids = [c.cell_id for c in phases.cells_for(p)]
    assert set(ids) == {"headline.mixed.gc-default", "headline.mixed.gc-generous"}


def test_unknown_scale_rejected():
    with pytest.raises(ValueError, match="unknown scale"):
        ScaleProfile.resolve("medium", load_slo())


# --------------------------------------------------------------------------- #
# no-crossing ladder path
# --------------------------------------------------------------------------- #
def test_no_crossing_ladder_then_fit_skips_math(isolated_results):
    ctx = _ctx("nc-1", runner=FakeRunner(crossing=False))
    phases.run_ladder(ctx, _GC_DEFAULT)
    lad = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")
    assert lad.state == State.DONE
    assert lad.data["crossed"] is False
    # bracket is the single top rung
    assert len(lad.data["bracket"]) == 1

    phases.run_fit(ctx, _GC_DEFAULT)
    fit = ctx.led.stage(_GC_DEFAULT.cell_id, "fit")
    assert fit.state == State.DONE
    assert fit.data["no_crossing"] is True
    # confirm runs at the top rung (the single-element bracket)
    assert fit.data["confirm_rates"] == lad.data["bracket"]


def test_crossing_ladder_brackets_two_rungs(isolated_results):
    # crossing=True grows p99 with rate; with start=100 step=2 the third rung
    # (400 rps) p99 = 4 + 4 = 8ms still < 20 at smoke (3 rungs). Use full-ish via
    # more rungs by faking a profile with enough rungs.
    ctx = _ctx("cr-1", runner=FakeRunner(crossing=True))
    # widen the ladder so a crossing actually occurs within range
    object.__setattr__(ctx.profile, "ladder_max_rungs", 12)
    object.__setattr__(ctx.profile, "ladder_step_factor", 1.6)
    phases.run_ladder(ctx, _GC_DEFAULT)
    lad = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")
    assert lad.data["crossed"] is True
    assert len(lad.data["bracket"]) == 2  # two bracketing rungs

    phases.run_fit(ctx, _GC_DEFAULT)
    fit = ctx.led.stage(_GC_DEFAULT.cell_id, "fit")
    assert fit.data["no_crossing"] is False
    assert "knee_rps" in fit.data
    # ladder reps (2/rung at smoke) < confirm_reps(10) -> CI flagged invalid
    assert fit.data["ci_valid"] is False


# --------------------------------------------------------------------------- #
# FIX 1: the pooled per-probe p99 (not the per-second-p99 percentile) drives
# the SLO crossing + fit; the per-second series is persisted for stationarity.
# --------------------------------------------------------------------------- #
def test_pooled_p99_drives_crossing_not_per_second_series(isolated_results):
    ctx = _ctx("pooled-1", runner=FakeRunner(crossing=True))
    object.__setattr__(ctx.profile, "ladder_max_rungs", 12)
    object.__setattr__(ctx.profile, "ladder_step_factor", 1.6)
    phases.run_ladder(ctx, _GC_DEFAULT)
    lad = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")

    # Both series are persisted: pooled p99_ms AND the per-second series.
    assert lad.data["p99_ms"], "pooled per-probe p99 must be persisted"
    assert lad.data["p99_series"], "per-second p99 series must still be persisted"
    # The rung means come from the POOLED p99 (4 + rate/100), NOT the ~1ms series.
    rungs = phases._collect_rungs(lad)
    for rate, mean_p99 in rungs:
        assert abs(mean_p99 - (4.0 + rate / 100.0)) < 1e-6
    # The crossing rung is the first whose pooled p99 >= 20 (rate >= 1600).
    crossed = [r for r, p in rungs if p >= ctx.slo_ms]
    assert crossed and crossed[0] >= 1600
    # stationarity still reads the per-second series (unchanged input).
    assert "stationarity" not in lad.data  # computed in run_fit, not ladder


def test_per_second_series_not_used_for_fit_input(isolated_results):
    """If the per-second series were the fit input the rungs would sit at ~1ms and
    never cross; the pooled p99 must be what brackets the SLO."""
    ctx = _ctx("pooled-2", runner=FakeRunner(crossing=True))
    object.__setattr__(ctx.profile, "ladder_max_rungs", 12)
    object.__setattr__(ctx.profile, "ladder_step_factor", 1.6)
    phases.run_ladder(ctx, _GC_DEFAULT)
    lad = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")
    assert lad.data["crossed"] is True
    by_rate = phases._rungs_by_rate(lad)
    # values are the pooled p99 (>> the ~1ms per-second series)
    assert all(all(v > 3.0 for v in vals) for vals in by_rate.values())


# --------------------------------------------------------------------------- #
# FIX 2: under-warmed probes are excluded from the fit/bracket; a rung emptied
# purely by exclusion raises PhaseError (never a silent hole).
# --------------------------------------------------------------------------- #
def test_under_warmed_probes_excluded_from_fit(isolated_results):
    # Flag ONE lang's probes under-warmed at the start rung; the other lang's
    # warm probes survive so the rung is not emptied.
    runner = FakeRunner(crossing=False, under_warm_on={("go", 100)})
    ctx = _ctx("uw-1", runner=runner)
    phases.run_ladder(ctx, _GC_DEFAULT)
    lad = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")
    # the under_warmed map records per-probe flags
    assert lad.data["under_warmed"]["r100.b0.go"] is True
    assert lad.data["under_warmed"]["r100.b0.dotnet"] is False
    # the excluded go probe is NOT in the rung's fit input at rate 100
    by_rate = phases._rungs_by_rate(lad)
    n_warm_at_100 = sum(1 for k, uw in lad.data["under_warmed"].items()
                        if k.startswith("r100.") and not uw)
    assert len(by_rate[100]) == n_warm_at_100
    # the per-probe pooled p99 map still records the under-warmed probe (raw data
    # is kept; exclusion happens only at fit-input assembly).
    assert "r100.b0.go" in lad.data["p99_ms"]


def test_fully_under_warmed_rung_raises(isolated_results):
    # Flag BOTH langs under-warmed at the start rung -> the rung empties on
    # exclusion -> PhaseError naming the rung + rate (no silent fit-around).
    runner = FakeRunner(crossing=False,
                        under_warm_on={("go", 100), ("dotnet", 100)})
    ctx = _ctx("uw-2", runner=runner)
    with pytest.raises(phases.PhaseError, match="100"):
        phases.run_ladder(ctx, _GC_DEFAULT)


# --------------------------------------------------------------------------- #
# FIX 4: core-parallelism manifest knobs (gomaxprocs / processor_count) +
# the Linux processor-count assertion.
# --------------------------------------------------------------------------- #
def test_record_cell_knobs_pins_gomaxprocs(isolated_results):
    # gc-default has empty extra_env, yet the pinned core knob must still land.
    ctx = _ctx("knobs-1", runner=FakeRunner(crossing=False))
    phases._record_cell_knobs(ctx, _GC_DEFAULT)
    assert ctx.manifest.knobs_go.gomaxprocs == phases.SUT_CORES
    # the fake runner exposes no /runtime-stats read -> processor_count stays None
    assert ctx.manifest.knobs_go.processor_count is None
    assert ctx.manifest.knobs_dotnet.processor_count is None


def test_record_cell_knobs_reads_processor_count(isolated_results):
    class PcRunner(FakeRunner):
        def read_processor_count(self, lang):
            return phases.SUT_CORES

    ctx = _ctx("knobs-2", runner=PcRunner(crossing=False))
    phases._record_cell_knobs(ctx, _GC_DEFAULT)
    assert ctx.manifest.knobs_go.processor_count == phases.SUT_CORES
    assert ctx.manifest.knobs_dotnet.processor_count == phases.SUT_CORES


def test_processor_count_mismatch_asserts_on_linux(isolated_results, monkeypatch):
    class BadPcRunner(FakeRunner):
        def read_processor_count(self, lang):
            return phases.SUT_CORES + 1  # wrong: not the pinned core count

    monkeypatch.setattr(phases, "IS_LINUX", True)
    ctx = _ctx("knobs-3", runner=BadPcRunner(crossing=False))
    with pytest.raises(phases.PhaseError, match="processor_count"):
        phases._record_cell_knobs(ctx, _GC_DEFAULT)


def test_processor_count_mismatch_records_without_asserting_off_linux(isolated_results, monkeypatch):
    class BadPcRunner(FakeRunner):
        def read_processor_count(self, lang):
            return phases.SUT_CORES + 1

    monkeypatch.setattr(phases, "IS_LINUX", False)
    ctx = _ctx("knobs-4", runner=BadPcRunner(crossing=False))
    phases._record_cell_knobs(ctx, _GC_DEFAULT)  # no raise on Windows smoke
    assert ctx.manifest.knobs_go.processor_count == phases.SUT_CORES + 1


def test_data_layer_mismatch_asserts_on_linux(isolated_results, monkeypatch):
    from types import SimpleNamespace

    class WrongDlRunner(FakeRunner):
        def read_build(self, lang, extra_env=None):
            # The env override "never reached the SUT": it still reports efcore.
            dl = "sqlc" if lang == "go" else "efcore"
            return SimpleNamespace(processor_count=phases.SUT_CORES, data_layer=dl)

    monkeypatch.setattr(phases, "IS_LINUX", True)
    ctx = _ctx("knobs-5", runner=WrongDlRunner(crossing=False))
    ado = next(c for c in phases.CELLS if c.cell_id == "variant.ado")
    with pytest.raises(phases.PhaseError, match="data_layer"):
        phases._record_cell_knobs(ctx, ado)


def test_data_layer_match_records_per_cell(isolated_results, monkeypatch):
    from types import SimpleNamespace

    class DlRunner(FakeRunner):
        def read_build(self, lang, extra_env=None):
            dl = "sqlc" if lang == "go" else (extra_env or {}).get("DATA_LAYER", "efcore")
            return SimpleNamespace(processor_count=phases.SUT_CORES, data_layer=dl)

    monkeypatch.setattr(phases, "IS_LINUX", True)
    ctx = _ctx("knobs-6", runner=DlRunner(crossing=False))
    dapper = next(c for c in phases.CELLS if c.cell_id == "variant.dapper")
    phases._record_cell_knobs(ctx, dapper)
    row = ctx.manifest.knobs_by_cell["variant.dapper"]
    assert row.go.data_layer == "sqlc"
    assert row.dotnet.data_layer == "dapper"


# --------------------------------------------------------------------------- #
# variants phase: declare-only by default, executes + pins a selection
# --------------------------------------------------------------------------- #
def test_variants_phase_declares_without_executing_by_default(isolated_results, monkeypatch):
    monkeypatch.delenv(phases.RUN_VARIANTS_ENV, raising=False)
    calls = []
    monkeypatch.setattr(phases, "run_cell", lambda ctx, cell: calls.append(cell.cell_id))
    ctx = _ctx("var-1", runner=FakeRunner(crossing=False))
    phases.run_variants_phase(ctx)
    assert calls == []
    rec = ctx.led.phase("variants")
    assert rec.state == phases.State.DONE
    assert rec.data["executed"] == []
    assert "variant.ado" in rec.data["declared"]
    assert "variant.dapper" in rec.data["declared"]


def test_variants_phase_executes_requested_and_pins_selection(isolated_results, monkeypatch):
    monkeypatch.setenv(phases.RUN_VARIANTS_ENV, "variant.ado,variant.dapper")
    monkeypatch.setattr(phases, "IS_LINUX", True)
    calls = []
    monkeypatch.setattr(phases, "run_cell", lambda ctx, cell: calls.append(cell.cell_id))
    ctx = _ctx("var-2", runner=FakeRunner(crossing=False))
    phases.run_variants_phase(ctx)
    assert calls == ["variant.ado", "variant.dapper"]
    rec = ctx.led.phase("variants")
    assert rec.data["requested"] == ["variant.ado", "variant.dapper"]
    assert rec.data["declared"]["variant.ado"]["deferred"] is False
    assert rec.data["declared"]["variant.prepared-parity"]["deferred"] is True

    # The pinned selection survives an env change on resume (phase already DONE
    # -> no re-execution; and the recorded list wins over the environment).
    monkeypatch.setenv(phases.RUN_VARIANTS_ENV, "variant.prepared-parity")
    phases.run_variants_phase(ctx)
    assert calls == ["variant.ado", "variant.dapper"]
    assert ctx.led.phase("variants").data["requested"] == ["variant.ado", "variant.dapper"]


def test_variants_phase_rejects_unknown_cell_without_pinning_it(isolated_results, monkeypatch):
    monkeypatch.setenv(phases.RUN_VARIANTS_ENV, "variant.nope")
    ctx = _ctx("var-3", runner=FakeRunner(crossing=False))
    with pytest.raises(phases.PhaseError, match="unknown variant"):
        phases.run_variants_phase(ctx)
    # The typo'd selection must NOT be pinned: after fixing the environment the
    # phase runs with the corrected list instead of re-raising forever.
    assert "requested" not in ctx.led.phase("variants").data
    monkeypatch.setenv(phases.RUN_VARIANTS_ENV, "")
    phases.run_variants_phase(ctx)
    assert ctx.led.phase("variants").state == phases.State.DONE


def test_run_all_pins_variant_selection_before_any_phase(isolated_results, monkeypatch):
    """Power-loss drill: the selection is pinned at run START, not at hour ~30.

    At full scale the variants phase begins after both headline cells; the
    documented crash recovery is a NAKED-shell `run-all --resume` (no env
    vars). If the pin only landed when the variants phase first executed, that
    resume would read an empty environment and silently mark every variant
    deferred: dropping the ~15 h variant cells with no error.
    """
    monkeypatch.setenv(phases.RUN_VARIANTS_ENV, "variant.ado")
    monkeypatch.setattr(phases, "IS_LINUX", True)
    pin_seen_at_seed = {}

    def crash_at_seed(ctx):
        # By the time ANY phase runs, the pin must already be on the ledger.
        pin_seen_at_seed["requested"] = ctx.led.phase("variants").data.get("requested")
        raise phases.PhaseError("simulated power loss")

    monkeypatch.setattr(phases, "run_doctor_phase", lambda ctx: None)
    monkeypatch.setattr(phases, "run_host_setup_phase", lambda ctx: None)
    monkeypatch.setattr(phases, "run_seed_phase", crash_at_seed)
    ctx = _ctx("var-runall-1")
    with pytest.raises(phases.PhaseError, match="power loss"):
        phases.run_all(ctx)
    assert pin_seen_at_seed["requested"] == ["variant.ado"]

    # Naked resume: env cleared, fresh context loads the ledger from disk.
    monkeypatch.delenv(phases.RUN_VARIANTS_ENV, raising=False)
    executed = []
    monkeypatch.setattr(phases, "run_seed_phase", lambda ctx: None)
    monkeypatch.setattr(phases, "run_capacity_gate_phase", lambda ctx: None)
    monkeypatch.setattr(phases, "run_validation_phase", lambda ctx: None)
    monkeypatch.setattr(phases, "run_cells_phase", lambda ctx: None)
    monkeypatch.setattr(phases, "finalize_manifest", lambda ctx: None)
    monkeypatch.setattr(phases, "run_host_restore_phase", lambda ctx: None)
    monkeypatch.setattr(phases, "run_cell", lambda ctx, cell: executed.append(cell.cell_id))
    ctx2 = _ctx("var-runall-1")
    phases.run_all(ctx2)
    assert executed == ["variant.ado"]
    assert ctx2.led.phase("variants").data["executed"] == ["variant.ado"]


def test_gate_degraded_optout_replays_from_ledger_stamp(isolated_results):
    """A resume shell WITHOUT the env var must not re-block a run whose gate
    already recorded the operator opt-out: the decision is run state."""
    from dataclasses import replace
    ctx = _ctx("gate-replay-1")
    ctx.profile = replace(ctx.profile, functional_only=False)
    ctx.led.mark_phase(
        "capacity_gate", phases.State.DONE, verdict="db-imposed-shared-knee",
        detail="insufficient headroom", degraded=True,
        degraded_allowed_by=phases.ALLOW_DB_IMPOSED_KNEE_ENV,
    )
    assert ctx.allow_db_imposed_knee is False
    phases._ensure_gate_applied(ctx)          # must not raise
    assert ctx.allow_db_imposed_knee is True


def test_gate_degraded_without_stamp_still_blocks_naked_resume(isolated_results):
    """No recorded opt-out + no env var: the degraded gate keeps blocking."""
    from dataclasses import replace
    ctx = _ctx("gate-replay-2")
    ctx.profile = replace(ctx.profile, functional_only=False)
    ctx.led.mark_phase(
        "capacity_gate", phases.State.DONE, verdict="db-imposed-shared-knee",
        detail="insufficient headroom", degraded=True,
    )
    with pytest.raises(phases.PhaseError, match="capacity gate verdict"):
        phases._ensure_gate_applied(ctx)


def test_gate_late_optin_on_resume_is_stamped(isolated_results):
    """Gate blocked the original start; the operator resumes WITH the env var:
    the decision must be recorded in ledger + manifest, not just acted on."""
    from dataclasses import replace
    ctx = _ctx("gate-replay-3")
    ctx.profile = replace(ctx.profile, functional_only=False)
    ctx.allow_db_imposed_knee = True
    ctx.led.mark_phase(
        "capacity_gate", phases.State.DONE, verdict="db-imposed-shared-knee",
        detail="insufficient headroom", degraded=True,
    )
    phases._ensure_gate_applied(ctx)          # proceeds via the opt-in
    data = ctx.led.phase("capacity_gate").data
    assert data["degraded_allowed_by"] == phases.ALLOW_DB_IMPOSED_KNEE_ENV
    assert ctx.manifest.capacity_gate.degraded is True
    assert ctx.manifest.capacity_gate.degraded_allowed_by == phases.ALLOW_DB_IMPOSED_KNEE_ENV


def test_variants_pinned_selection_beats_env_on_crash_resume(isolated_results, monkeypatch):
    # Simulated crash: the selection is pinned, one cell "ran", the phase is NOT
    # done. A resume under a DIFFERENT environment must replay the pinned list.
    monkeypatch.setenv(phases.RUN_VARIANTS_ENV, "variant.ado")
    monkeypatch.setattr(phases, "IS_LINUX", True)
    calls = []
    monkeypatch.setattr(phases, "run_cell", lambda ctx, cell: calls.append(cell.cell_id))
    ctx = _ctx("var-4", runner=FakeRunner(crossing=False))
    assert phases._requested_variants(ctx) == ["variant.ado"]   # pin, then "crash"

    monkeypatch.setenv(phases.RUN_VARIANTS_ENV, "variant.dapper,variant.prepared-parity")
    phases.run_variants_phase(ctx)
    assert calls == ["variant.ado"]
    assert ctx.led.phase("variants").data["requested"] == ["variant.ado"]


# --------------------------------------------------------------------------- #
# skip-if-done
# --------------------------------------------------------------------------- #
def test_manifest_records_real_achieved_rate(isolated_results):
    runner = FakeRunner(crossing=False)
    ctx = _ctx("mani-1", runner=runner)
    phases.run_ladder(ctx, _GC_DEFAULT)
    rows = ctx.manifest.achieved_vs_offered
    assert rows, "expected per-probe achieved-vs-offered rows"
    # FakeRunner achieves rate*0.999 -> achieved_rate must be that, NOT 0.0
    r = rows[0]
    assert r.achieved_rate == r.offered_rate * 0.999
    assert abs(r.ratio - 0.999) < 1e-6


def test_skip_if_done_does_not_rerun_probes(isolated_results):
    runner = FakeRunner(crossing=False)
    ctx = _ctx("skip-1", runner=runner)
    phases.run_ladder(ctx, _GC_DEFAULT)
    first = runner.calls
    assert first > 0
    # second call with the SAME ledger: stage is done -> no new probes
    phases.run_ladder(ctx, _GC_DEFAULT)
    assert runner.calls == first


def test_skip_if_done_survives_reload(isolated_results):
    runner = FakeRunner(crossing=False)
    ctx = _ctx("skip-2", runner=runner)
    phases.run_cell(ctx, _GC_DEFAULT)
    n = runner.calls
    # reload the ledger fresh and re-run the cell: everything done -> zero probes
    ctx2 = _ctx("skip-2", runner=runner)
    phases.run_cell(ctx2, _GC_DEFAULT)
    assert runner.calls == n


# --------------------------------------------------------------------------- #
# RCB block-reset on resume
# --------------------------------------------------------------------------- #
def test_block_reset_flips_orphan_done_member(isolated_results):
    led = Ledger.load_or_create("blk-1", config_hash="c", rng_seed=42)
    cell_id, stage = "headline.mixed.gc-default", "confirm"
    s = led.stage(cell_id, stage)
    # block 0: both done; block 1: ONE done (orphan); block 2: both pending
    for block in range(3):
        for lang in ("go", "dotnet"):
            s.probes.append(ProbeRecord(block=block, slot_order=["go", "dotnet"], lang=lang, rate=8120))
    led.mark_probe_done(cell_id, stage, s.probes[0], artifact="b0.go", achieved=1, error_rate=0)
    led.mark_probe_done(cell_id, stage, s.probes[1], artifact="b0.dn", achieved=1, error_rate=0)
    led.mark_probe_done(cell_id, stage, s.probes[2], artifact="b1.go", achieved=1, error_rate=0)  # orphan

    reset = led.reset_orphan_blocks(cell_id, stage)
    assert reset == [1]
    # block 0 stays done; block 1's done member is flipped back to pending
    states = {(p.block, p.lang): p.state for p in led.stage(cell_id, stage).probes}
    assert states[(0, "go")] == State.DONE and states[(0, "dotnet")] == State.DONE
    assert states[(1, "go")] == State.PENDING and states[(1, "dotnet")] == State.PENDING
    assert led.stage(cell_id, stage).data["redone_after_resume"] is True
    assert led.stage(cell_id, stage).data["redone_blocks"] == [1]


def test_block_reset_idempotent_and_keyed_by_rate(isolated_results):
    led = Ledger.load_or_create("blk-2", config_hash="c", rng_seed=42)
    cell_id, stage = "c", "confirm"
    s = led.stage(cell_id, stage)
    # two rates; an orphan only at rate 9000 -> rate 8000 block must be untouched
    for rate in (8000, 9000):
        for lang in ("go", "dotnet"):
            s.probes.append(ProbeRecord(block=0, slot_order=["go", "dotnet"], lang=lang, rate=rate))
    # rate 8000: both done (complete). rate 9000: one done (orphan).
    led.mark_probe_done(cell_id, stage, s.probes[0], artifact="a", achieved=1, error_rate=0)
    led.mark_probe_done(cell_id, stage, s.probes[1], artifact="b", achieved=1, error_rate=0)
    led.mark_probe_done(cell_id, stage, s.probes[2], artifact="c", achieved=1, error_rate=0)
    reset = led.reset_orphan_blocks(cell_id, stage)
    assert reset == [0]  # block 0 at rate 9000
    states = {(p.rate, p.lang): p.state for p in led.stage(cell_id, stage).probes}
    # the complete 8000 block survives untouched
    assert states[(8000, "go")] == State.DONE and states[(8000, "dotnet")] == State.DONE
    assert states[(9000, "go")] == State.PENDING
    # a second call is a no-op (nothing orphaned now)
    assert led.reset_orphan_blocks(cell_id, stage) == []


def test_block_reset_called_on_confirm_resume(isolated_results):
    """A confirm stage resumed with an orphan re-runs the whole block."""
    runner = FakeRunner(crossing=False)
    ctx = _ctx("blk-resume", runner=runner)
    # set up: ladder + fit done, confirm partially executed with an orphan.
    phases.run_ladder(ctx, _GC_DEFAULT)
    phases.run_fit(ctx, _GC_DEFAULT)
    # plan the confirm probes by calling run_confirm once, but inject a stop:
    # simulate a crash by marking only ONE probe of block 0 done, rest pending.
    cid = _GC_DEFAULT.cell_id
    conf = ctx.led.stage(cid, "confirm")
    rates = [int(r) for r in ctx.led.stage(cid, "fit").data["confirm_rates"]]
    for ridx, rate in enumerate(rates):
        for p in phases.plan_blocks(n_blocks=ctx.profile.confirm_blocks, rate=rate,
                                    rng_seed=ctx.led.rng_seed,
                                    salt=phases._salt(cid, "confirm", ridx)):
            conf.probes.append(p)
    ctx.led.mark_stage(cid, "confirm", State.RUNNING)
    # mark first probe of block 0 done (orphan)
    ctx.led.mark_probe_done(cid, "confirm", conf.probes[0], artifact="x", achieved=1, error_rate=0)
    runner.calls = 0

    # now resume confirm: orphan block 0 must be reset (redone), all probes run
    phases.run_confirm(ctx, _GC_DEFAULT)
    assert ctx.led.stage(cid, "confirm").state == State.DONE
    assert ctx.led.stage(cid, "confirm").data.get("redone_after_resume") is True
    # every probe ran (the orphan was flipped back and re-run)
    assert all(p.state == State.DONE for p in ctx.led.stage(cid, "confirm").probes)


# --------------------------------------------------------------------------- #
# RCB slot order is seeded + recorded
# --------------------------------------------------------------------------- #
def test_plan_blocks_records_seeded_slot_order(isolated_results):
    a = phases.plan_blocks(n_blocks=4, rate=1000, rng_seed=42, salt=7)
    b = phases.plan_blocks(n_blocks=4, rate=1000, rng_seed=42, salt=7)
    # deterministic given (seed, salt)
    assert [(p.block, p.lang) for p in a] == [(p.block, p.lang) for p in b]
    # each block holds exactly {go, dotnet}
    by_block: dict[int, set[str]] = {}
    for p in a:
        by_block.setdefault(p.block, set()).add(p.lang)
        assert p.slot_order in (["go", "dotnet"], ["dotnet", "go"])
    assert all(v == {"go", "dotnet"} for v in by_block.values())
    # a different salt yields a different order stream (not identical)
    c = phases.plan_blocks(n_blocks=8, rate=1000, rng_seed=42, salt=99)
    assert [p.slot_order for p in c] != [p.slot_order for p in a + a]


# --------------------------------------------------------------------------- #
# probe failure surfaces loudly
# --------------------------------------------------------------------------- #
def test_probe_failure_raises_phase_error(isolated_results):
    runner = FakeRunner(crossing=False, fail_on={("go", 100)})
    ctx = _ctx("fail-1", runner=runner)
    with pytest.raises(phases.PhaseError, match="probe failed"):
        phases.run_ladder(ctx, _GC_DEFAULT)
    # the failing probe is recorded FAILED (not silently skipped)
    failed = [p for p in ctx.led.stage(_GC_DEFAULT.cell_id, "ladder").probes
              if p.state == State.FAILED]
    assert failed and failed[0].lang == "go"


# --------------------------------------------------------------------------- #
# seed verify-only-on-resume gating (the count-divergence fix)
# --------------------------------------------------------------------------- #
def test_any_cell_probe_done_detects_writes(isolated_results):
    ctx = _ctx("seed-gate-1")
    assert phases._any_cell_probe_done(ctx.led) is False
    # add a done probe to a cell ladder
    from ledgerbench.ledger import ProbeRecord
    s = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")
    s.probes.append(ProbeRecord(block=0, slot_order=["go", "dotnet"], lang="go", rate=100))
    ctx.led.mark_probe_done(_GC_DEFAULT.cell_id, "ladder", s.probes[0],
                            artifact="x", achieved=99, error_rate=0)
    assert phases._any_cell_probe_done(ctx.led) is True


def test_seed_resume_skips_verify_only_after_writes(isolated_results, monkeypatch):
    """A resumed seed must NOT run the count-invariant preflight once write-probes
    have run (the seeded counts diverge by design). It records the skip reason and
    never touches docker."""
    ctx = _ctx("seed-gate-2")
    ctx.led.mark_phase("seed", State.DONE, scale="smoke")  # seed already done
    from ledgerbench.ledger import ProbeRecord
    s = ctx.led.stage(_GC_DEFAULT.cell_id, "confirm")
    s.probes.append(ProbeRecord(block=0, slot_order=["go", "dotnet"], lang="go", rate=100))
    ctx.led.mark_probe_done(_GC_DEFAULT.cell_id, "confirm", s.probes[0],
                            artifact="x", achieved=99, error_rate=0)

    # if docker were touched the test env has no seeder; assert it is NOT called.
    called = {"compose": False}

    def _boom(*a, **k):
        called["compose"] = True
        raise AssertionError("compose must not be called on the verify-only skip path")

    monkeypatch.setattr(phases.compose, "compose", _boom)
    phases.run_seed_phase(ctx)
    assert called["compose"] is False
    assert ctx.led.phase_done("seed")
    assert "verify_only_skipped" in ctx.led.phase("seed").data


# --------------------------------------------------------------------------- #
# capacity-gate divergence: verify-only preflight must NOT run after the gate
# wrote invoices (exact-count invariants no longer hold).
# --------------------------------------------------------------------------- #
def test_capacity_gate_counts_as_db_divergence(isolated_results):
    led = Ledger.load_or_create("cgd-1", config_hash="c", rng_seed=1)
    assert phases._db_has_diverged(led) == (False, "")
    # RUNNING (mid-gate crash window) already counts as diverged.
    led.mark_phase("capacity_gate", State.RUNNING)
    diverged, why = phases._db_has_diverged(led)
    assert diverged and "capacity_gate" in why
    led.mark_phase("capacity_gate", State.DONE)
    assert phases._db_has_diverged(led)[0] is True


def test_seed_verify_only_skipped_after_capacity_gate(isolated_results, monkeypatch):
    ctx = _ctx("cgd-2")
    ctx.led.mark_phase("seed", State.DONE, scale="smoke")
    ctx.led.mark_phase("capacity_gate", State.RUNNING)  # crashed mid-gate

    def _boom(*a, **k):
        raise AssertionError("seeder must not run verify-only after gate writes")

    monkeypatch.setattr(phases.compose, "compose", _boom)
    phases.run_seed_phase(ctx)
    assert ctx.led.phase_done("seed")
    assert "capacity_gate" in ctx.led.phase("seed").data["verify_only_skipped"]


# --------------------------------------------------------------------------- #
# scale is pinned on the ledger like config_hash
# --------------------------------------------------------------------------- #
def test_resume_scale_mismatch_refused(isolated_results):
    Ledger.load_or_create("sc-1", config_hash="c", rng_seed=1, scale="smoke")
    with pytest.raises(RuntimeError, match="scale mismatch"):
        Ledger.load_or_create("sc-1", config_hash="c", rng_seed=1, scale="full")
    # same scale resumes; scale=None (legacy caller) skips the check
    assert Ledger.load_or_create("sc-1", config_hash="c", rng_seed=1, scale="smoke").scale == "smoke"
    assert Ledger.load_or_create("sc-1", config_hash="c", rng_seed=1).scale == "smoke"


def test_resume_scale_backfilled_on_legacy_ledger(isolated_results):
    led = Ledger.load_or_create("sc-2", config_hash="c", rng_seed=1)  # pre-field
    assert led.scale is None
    led2 = Ledger.load_or_create("sc-2", config_hash="c", rng_seed=1, scale="full")
    assert led2.scale == "full"


# --------------------------------------------------------------------------- #
# measurement gates: error-rate / achieved-vs-offered exclusion (full scale)
# --------------------------------------------------------------------------- #
def test_error_rate_excludes_probe_from_fit(isolated_results):
    class ErrRunner(FakeRunner):
        def run_probe(self, **kw):
            out = super().run_probe(**kw)
            if kw["lang"] == "go" and kw["rate"] == 100:
                out.error_rate = 0.5  # way over slo error_rate_max (0.001)
            return out

    ctx = _ctx("er-1", runner=ErrRunner(crossing=False))
    object.__setattr__(ctx.profile, "functional_only", False)  # gates are full-scale
    # measured scale => the stage guard demands a capacity-gate verdict
    ctx.led.mark_phase("capacity_gate", State.DONE, verdict="pass")
    phases.run_ladder(ctx, _GC_DEFAULT)
    lad = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")
    excluded = lad.data.get("excluded", {})
    assert any(k.startswith("r100.") and k.endswith(".go") for k in excluded)
    by_rate, empty = phases._rung_values(lad)
    # go probes at rung 100 are excluded; the dotnet reps keep the rung alive
    n_dotnet_100 = sum(1 for k in lad.data["p99_ms"]
                       if k.startswith("r100.") and k.endswith(".dotnet"))
    assert len(by_rate[100]) == n_dotnet_100
    assert not empty


def test_smoke_records_but_does_not_exclude_on_error_rate(isolated_results):
    class ErrRunner(FakeRunner):
        def run_probe(self, **kw):
            out = super().run_probe(**kw)
            out.error_rate = 0.5
            return out

    ctx = _ctx("er-2", runner=ErrRunner(crossing=False))  # smoke: functional only
    phases.run_ladder(ctx, _GC_DEFAULT)
    lad = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")
    assert lad.state == State.DONE
    assert not lad.data.get("excluded")


# --------------------------------------------------------------------------- #
# fully-excluded rung: one-shot retry, then loud failure
# --------------------------------------------------------------------------- #
def test_fully_under_warmed_rung_retried_once_then_succeeds(isolated_results):
    class TransientWarm(FakeRunner):
        def __init__(self):
            super().__init__(crossing=False)
            self.attempts: dict[tuple, int] = {}

        def run_probe(self, **kw):
            out = super().run_probe(**kw)
            key = (kw["lang"], kw["rate"], kw["block"])  # per-probe attempts
            self.attempts[key] = self.attempts.get(key, 0) + 1
            if kw["rate"] == 100 and self.attempts[key] == 1:
                out.under_warmed = True  # transient: clean on the retry
            return out

    ctx = _ctx("rw-1", runner=TransientWarm())
    phases.run_ladder(ctx, _GC_DEFAULT)
    lad = ctx.led.stage(_GC_DEFAULT.cell_id, "ladder")
    assert lad.state == State.DONE
    assert lad.data.get("rewarmed_rungs") == [100]
    by_rate, empty = phases._rung_values(lad)
    assert 100 in by_rate and not empty
    # manifest rows replaced, not duplicated, across the rung re-run
    ids = [r.probe_id for r in ctx.manifest.achieved_vs_offered]
    assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------- #
# capacity gate: a failed gate STOPS the run, and fired levers actually apply
#. These drive run_capacity_gate_phase on the Linux path with a fake
# runner: no docker, no PG.
# --------------------------------------------------------------------------- #
class GateRunner(FakeRunner):
    """FakeRunner + the capacity-gate surface (knee ladder, lever application)."""

    def __init__(self, knees):
        super().__init__(crossing=False)
        self._knees = list(knees)          # one PgKneeMeasurement per call
        self.workload = load_workload()
        self.env_overrides: dict[str, str] = {}
        self.knee_calls: list[int | None] = []
        self.topology_calls = 0
        self.apply_calls = 0

    def measure_pg_create_knee(self, *, out_dir, rungs, window_s, pg_cores=None):
        self.knee_calls.append(pg_cores)
        return self._knees.pop(0) if len(self._knees) > 1 else self._knees[0]

    def apply_effective(self, *, workload=None, env_overrides=None):
        self.apply_calls += 1
        if workload is not None:
            self.workload = workload
        if env_overrides:
            self.env_overrides.update(env_overrides)

    def apply_topology(self):
        self.topology_calls += 1
        return {"recreated": ["postgres", "vegeta"], "cpusets": dict(self.env_overrides)}


def _measured_knee(rps: float, *, cores: int = 3):
    return capgate.PgKneeMeasurement(
        knee_rps=rps, usable_rungs=4, total_rungs=5, measured_at_cores=cores,
        detail="knee = top usable rung",
    )


def _unmeasured_knee():
    # exactly the 2026-06-06 shape: the create-only ladder broke on rung one
    return capgate.pg_knee_from_rungs(
        [capgate.RungResult(rate=500, achieved=500.0057, p99_ms=4.0, error_rate=0.0)]
    )


def _gate_ctx(run_id, runner, *, monkeypatch, allow=False):
    """Measured-scale gate context on the Linux path, with tiny targets gen."""
    monkeypatch.setattr(phases, "IS_LINUX", True)
    ctx = _ctx(run_id, runner=runner)
    ctx.allow_db_imposed_knee = allow
    # smoke numerics, but NOT functional-only: the gate must enforce.
    object.__setattr__(ctx.profile, "functional_only", False)
    object.__setattr__(ctx.profile, "target_count", 20)
    return ctx


def _needed_knee(ctx, *, write_share=None, cache_hit_rate=None):
    """PG knee that exactly meets headroom for the given lever state."""
    p = capgate.ResolvedWorkloadParams.from_workload(ctx.workload)
    if write_share is not None:
        p.write_share = write_share
    if cache_hit_rate is not None:
        p.cache_hit_rate = cache_hit_rate
    projected_knee = float(ctx.profile.ladder_start_rps) * 8.0
    return (capgate.projected_db_load_rps(projected_knee, p)
            * ctx.slo.capacity_gate.headroom_factor)


def test_unmeasured_pg_knee_aborts_the_run(isolated_results, monkeypatch):
    runner = GateRunner([_unmeasured_knee()])
    ctx = _gate_ctx("gate-nok", runner, monkeypatch=monkeypatch)
    with pytest.raises(phases.PhaseError, match="no-pg-knee-measured"):
        phases.run_capacity_gate_phase(ctx)
    data = ctx.led.phase("capacity_gate").data
    assert data["verdict"] == "no-pg-knee-measured"
    assert data["pg_knee_measured"] is False
    assert data["pg_knee_rps"] is None          # never a fallback number
    assert ctx.manifest.capacity_gate.degraded is True


def test_failed_gate_aborts_before_any_cell(isolated_results, monkeypatch):
    # PG far too small for the projected load; every lever exhausts.
    runner = GateRunner([_measured_knee(1.0)])
    ctx = _gate_ctx("gate-fail", runner, monkeypatch=monkeypatch)
    with pytest.raises(phases.PhaseError, match="db-imposed-shared-knee"):
        phases.run_capacity_gate_phase(ctx)
    assert ctx.manifest.capacity_gate.verdict == "db-imposed-shared-knee"
    assert ctx.manifest.capacity_gate.degraded is True
    assert ctx.manifest.capacity_gate.degraded_allowed_by is None
    # no cell probe may have run
    assert phases._any_cell_probe_done(ctx.led) is False


def test_failed_gate_opt_out_stamps_degraded_everywhere(isolated_results, monkeypatch):
    runner = GateRunner([_measured_knee(1.0)])
    ctx = _gate_ctx("gate-degraded", runner, monkeypatch=monkeypatch, allow=True)
    phases.run_capacity_gate_phase(ctx)            # no raise: explicit opt-out
    data = ctx.led.phase("capacity_gate").data
    assert data["degraded"] is True
    assert data["degraded_allowed_by"] == phases.ALLOW_DB_IMPOSED_KNEE_ENV
    assert ctx.manifest.capacity_gate.degraded is True
    assert ctx.manifest.capacity_gate.degraded_allowed_by == phases.ALLOW_DB_IMPOSED_KNEE_ENV


def test_opt_out_env_var_is_read_by_make_context(monkeypatch):
    monkeypatch.setenv(phases.ALLOW_DB_IMPOSED_KNEE_ENV, "1")
    assert phases._env_flag(phases.ALLOW_DB_IMPOSED_KNEE_ENV) is True
    monkeypatch.setenv(phases.ALLOW_DB_IMPOSED_KNEE_ENV, "0")
    assert phases._env_flag(phases.ALLOW_DB_IMPOSED_KNEE_ENV) is False
    monkeypatch.delenv(phases.ALLOW_DB_IMPOSED_KNEE_ENV, raising=False)
    assert phases._env_flag(phases.ALLOW_DB_IMPOSED_KNEE_ENV) is False


def test_fired_write_share_lever_reaches_workload_and_runner(isolated_results, monkeypatch):
    runner = GateRunner([])
    ctx = _gate_ctx("gate-lever", runner, monkeypatch=monkeypatch)
    runner._knees = [_measured_knee(_needed_knee(ctx, write_share=0.10))]
    phases.run_capacity_gate_phase(ctx)

    assert ctx.led.phase("capacity_gate").data["verdict"] == "pass"
    assert ctx.led.phase("capacity_gate").data["levers_applied"] == ["write_share"]
    # the EFFECTIVE workload is what the cells will generate targets from
    assert ctx.workload.mix.create_invoice == 10
    assert ctx.spec_workload.mix.create_invoice == 15      # spec untouched
    assert runner.workload.mix.create_invoice == 10        # runner adopted it
    eff = ctx.manifest.effective_workload
    assert eff.spec_write_share == 0.15 and eff.effective_write_share == 0.10
    assert eff.differs_from_spec is True
    # and the ledger can replay it on resume
    assert ctx.led.phase("capacity_gate").data["effective"]["write_share"] == 0.10


def test_lever_that_cannot_be_applied_fails_the_gate(isolated_results, monkeypatch):
    runner = GateRunner([])
    ctx = _gate_ctx("gate-unapplied", runner, monkeypatch=monkeypatch)
    # needs BOTH soft levers: cache_hit_rate has no pre-registered TTL, so it
    # fires but cannot be realized -> the PASS would be a paper pass.
    runner._knees = [_measured_knee(_needed_knee(ctx, write_share=0.10, cache_hit_rate=0.90))]
    monkeypatch.delenv(phases.CACHE_TTL_OVERRIDE_ENV, raising=False)
    with pytest.raises(phases.PhaseError, match="lever-not-applied"):
        phases.run_capacity_gate_phase(ctx)
    assert "cache_hit_rate" in ctx.manifest.capacity_gate.levers_not_applied
    # the unapplied lever must NOT show up as an effective change
    assert ctx.manifest.effective_workload.effective_cache_hit_rate == 0.80


def test_cache_ttl_override_makes_the_lever_real(isolated_results, monkeypatch):
    runner = GateRunner([])
    ctx = _gate_ctx("gate-ttl", runner, monkeypatch=monkeypatch)
    runner._knees = [_measured_knee(_needed_knee(ctx, write_share=0.10, cache_hit_rate=0.90))]
    monkeypatch.setenv(phases.CACHE_TTL_OVERRIDE_ENV, "900")
    phases.run_capacity_gate_phase(ctx)
    assert ctx.manifest.capacity_gate.verdict == "pass"
    assert runner.env_overrides["CACHE_TTL_SECONDS"] == "900"
    assert ctx.workload.cache_construction.target_hit_rate == 0.90


def test_pg_cores_lever_moves_cores_remeasures_and_can_still_fail(isolated_results, monkeypatch):
    runner = GateRunner([])
    ctx = _gate_ctx("gate-cores", runner, monkeypatch=monkeypatch)
    monkeypatch.setenv(phases.CACHE_TTL_OVERRIDE_ENV, "900")
    need = _needed_knee(ctx, write_share=0.10, cache_hit_rate=0.90)
    # 3-core knee that only clears headroom under the 4/3 linear projection ...
    runner._knees = [_measured_knee(need * 3.0 / 4.0 + 1.0),
                     # ... but the RE-MEASURE at 4 cores says otherwise.
                     _measured_knee(need * 0.5, cores=4)]
    with pytest.raises(phases.PhaseError, match="db-imposed-shared-knee"):
        phases.run_capacity_gate_phase(ctx)
    assert runner.env_overrides["PG_CPUS"] == "8-11"
    assert runner.topology_calls == 1               # PG recreated on new cpuset
    assert runner.knee_calls == [None, 4]           # re-measured at the new cores
    assert ctx.manifest.capacity_gate.remeasured_pg_knee_rps == pytest.approx(need * 0.5)


def test_resume_reapplies_the_effective_workload(isolated_results, monkeypatch):
    runner = GateRunner([])
    ctx = _gate_ctx("gate-resume", runner, monkeypatch=monkeypatch)
    runner._knees = [_measured_knee(_needed_knee(ctx, write_share=0.10))]
    phases.run_capacity_gate_phase(ctx)

    # fresh process: ledger reloaded, workload back at the frozen spec values
    runner2 = GateRunner([_measured_knee(1.0)])   # must NOT be called again
    ctx2 = _gate_ctx("gate-resume", runner2, monkeypatch=monkeypatch)
    assert ctx2.workload.mix.create_invoice == 15
    phases.run_capacity_gate_phase(ctx2)          # phase is done -> replay only
    assert runner2.knee_calls == []               # the ladder did not re-run
    assert ctx2.workload.mix.create_invoice == 10
    assert runner2.workload.mix.create_invoice == 10


def test_resume_replays_a_stamped_gate_optout(isolated_results, monkeypatch):
    runner = GateRunner([_measured_knee(1.0)])
    ctx = _gate_ctx("gate-resume-fail", runner, monkeypatch=monkeypatch, allow=True)
    phases.run_capacity_gate_phase(ctx)           # recorded degraded + stamped

    # Resuming WITHOUT the env var must NOT re-block: the opt-out was stamped
    # into the ledger at the first start and is run state, not shell state
    # (the documented crash recovery is a naked shell).
    ctx2 = _gate_ctx("gate-resume-fail", GateRunner([]), monkeypatch=monkeypatch)
    phases.run_capacity_gate_phase(ctx2)          # no raise
    assert ctx2.allow_db_imposed_knee is True


def test_resume_re_enforces_an_unstamped_failed_gate(isolated_results, monkeypatch):
    runner = GateRunner([_measured_knee(1.0)])
    ctx = _gate_ctx("gate-resume-fail2", runner, monkeypatch=monkeypatch, allow=False)
    with pytest.raises(phases.PhaseError, match="db-imposed-shared-knee"):
        phases.run_capacity_gate_phase(ctx)       # recorded degraded, UNstamped

    # No opt-out was ever given: the degraded gate keeps blocking on resume.
    ctx2 = _gate_ctx("gate-resume-fail2", GateRunner([]), monkeypatch=monkeypatch)
    with pytest.raises(phases.PhaseError, match="db-imposed-shared-knee"):
        phases.run_capacity_gate_phase(ctx2)


def test_smoke_records_the_verdict_without_aborting(isolated_results, monkeypatch):
    """A functional rehearsal exercises the path but publishes nothing."""
    monkeypatch.setattr(phases, "IS_LINUX", True)
    runner = GateRunner([_measured_knee(1.0)])
    ctx = _ctx("gate-smoke", runner=runner)       # smoke: functional_only
    object.__setattr__(ctx.profile, "target_count", 20)
    phases.run_capacity_gate_phase(ctx)           # no raise at smoke scale
    assert ctx.led.phase("capacity_gate").data["verdict"] == "db-imposed-shared-knee"


# --------------------------------------------------------------------------- #
# F8: knobs are recorded PER CELL and never cross-copied between languages
# --------------------------------------------------------------------------- #
_GC_GENEROUS = next(c for c in phases.CELLS if c.cell_id == "headline.mixed.gc-generous")


def test_cell_knobs_are_recorded_per_cell(isolated_results):
    ctx = _ctx("knobs-cell-1", runner=FakeRunner(crossing=False))
    phases._record_cell_knobs(ctx, _GC_DEFAULT)
    phases._record_cell_knobs(ctx, _GC_GENEROUS)
    by_cell = ctx.manifest.knobs_by_cell
    assert set(by_cell) == {_GC_DEFAULT.cell_id, _GC_GENEROUS.cell_id}
    # the defaults cell keeps NO gc overrides even though a later cell set them
    assert by_cell[_GC_DEFAULT.cell_id].go.gogc is None
    assert by_cell[_GC_DEFAULT.cell_id].dotnet.gc_dynamic_adaptation_mode is None
    assert by_cell[_GC_GENEROUS.cell_id].go.gogc == "off"
    assert by_cell[_GC_GENEROUS.cell_id].dotnet.gc_dynamic_adaptation_mode == "0"
    # the verbatim cell env is echoed too
    assert by_cell[_GC_GENEROUS.cell_id].extra_env == _GC_GENEROUS.extra_env


def test_cell_knobs_are_not_cross_copied_between_languages(isolated_results):
    ctx = _ctx("knobs-cell-2", runner=FakeRunner(crossing=False))
    phases._record_cell_knobs(ctx, _GC_GENEROUS)
    row = ctx.manifest.knobs_by_cell[_GC_GENEROUS.cell_id]
    # Go-only knobs never land on the .NET row ...
    assert row.dotnet.gogc is None
    assert row.dotnet.gomemlimit is None
    assert row.dotnet.gomaxprocs is None
    # ... and .NET-only knobs never land on the Go row.
    assert row.go.gc_dynamic_adaptation_mode is None
    assert row.go.gogc == "off" and row.go.gomemlimit == "10GiB"
    assert row.go.gomaxprocs == phases.SUT_CORES


def test_variant_dotnet_knobs_stay_on_the_dotnet_row(isolated_results):
    ctx = _ctx("knobs-cell-3", runner=FakeRunner(crossing=False))
    ado = next(c for c in phases.CELLS if c.cell_id == "variant.ado")
    phases._record_cell_knobs(ctx, ado)
    row = ctx.manifest.knobs_by_cell["variant.ado"]
    assert row.dotnet.data_layer == "ado"
    assert row.go.data_layer is None


# --------------------------------------------------------------------------- #
# F4: the effective working set is recorded (distinct ids + replay period)
# --------------------------------------------------------------------------- #
def test_working_set_records_distinct_ids_and_replay_period(isolated_results):
    from ledgerbench import vegeta

    ctx = _ctx("ws-1", runner=FakeRunner(crossing=False))
    io_dir = paths.run_dir("ws-1") / "io" / "go"
    vegeta.generate_targets(
        ctx.workload, out_dir=io_dir, count=200,
        seed=ctx.workload.rng.target_generator_seed_default, scale="smoke",
    )
    phases._record_working_set(ctx)
    ws = ctx.manifest.working_set
    assert ws.target_count == 200
    assert 0 < ws.distinct_invoice_ids <= 200
    assert 0 < ws.distinct_customer_ids <= 200
    # seeded scale is recorded next to it so "seeded" can never read as "measured"
    assert ws.seeded_invoices == ctx.workload.seeding.scales["smoke"].invoices
    assert ws.distinct_invoice_ids < ws.seeded_invoices
    phases._record_replay_period(ctx, 100)
    assert ws.replay_period_seconds["100"] == 2.0


# --------------------------------------------------------------------------- #
# F8: manifest aggregates are built from the artifacts already on disk, and say
# "not measured" when nothing is there.
# --------------------------------------------------------------------------- #
def _write_probe_artifacts(run_id, *, lang="go", faults=True, cache=True):
    import json as _json

    d = paths.run_dir(run_id) / "cells" / "c" / "ladder" / "rate100" / "block0"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"cpu_mem_{lang}.summary.json").write_text(_json.dumps({
        "container": f"ledgerline-sut-{lang}-1", "enabled": True,
        "cpu_usage_delta_usec": 1000, "nr_throttled_end": 2, "throttled_usec_end": 30,
        "mem_anon_p99": 500, "mem_peak": 600, "oom_events": 0,
        "major_faults_delta": 7 if faults else None,
        "minor_faults_delta": 9 if faults else None,
    }))
    (d / f"{lang}.summary.json").write_text(_json.dumps({
        "requests": 1000, "rate": 99.9, "success": 0.999,
        "status_codes": {"200": 900, "201": 98, "500": 2},
        "latencies": {"99th": 5_000_000},
    }))
    if cache:
        (d / f"cache_stats_{lang}.json").write_text(_json.dumps({
            "keyspace_hits": 80, "keyspace_misses": 20, "hit_rate": 0.8,
            "source": "redis INFO stats delta over the measured window",
        }))


def test_finalize_manifest_aggregates_probe_artifacts(isolated_results):
    ctx = _ctx("agg-1", runner=FakeRunner(crossing=False))
    _write_probe_artifacts("agg-1")
    phases.finalize_manifest(ctx)
    m = ctx.manifest
    assert m.resource_deltas[0].cpu_usage_usec_delta == 1000
    assert m.resource_deltas[0].windows == 1
    assert m.throttle_counters[0].nr_throttled == 2
    assert m.fault_counts[0].major_faults == 7
    assert m.fault_counts[0].source != "not_measured"
    # errors come from the vegeta status codes, not from a default zero
    assert m.errors.count == 2
    assert m.errors.total_requests == 1000
    assert m.errors.rate == pytest.approx(0.002)
    assert m.errors.status_codes["500"] == 2
    assert m.cache_stats.keyspace_hits == 80
    assert m.cache_stats.hit_rate == pytest.approx(0.8)


def test_finalize_manifest_says_not_measured_instead_of_zero(isolated_results):
    ctx = _ctx("agg-2", runner=FakeRunner(crossing=False))
    _write_probe_artifacts("agg-2", faults=False, cache=False)
    phases.finalize_manifest(ctx)
    # faults: null, NOT 0, and explicitly labeled
    assert ctx.manifest.fault_counts[0].major_faults is None
    assert ctx.manifest.fault_counts[0].source == "not_measured"
    assert ctx.manifest.cache_stats.keyspace_hits is None
    assert ctx.manifest.cache_stats.source == "not_measured"
    assert "NOT a measured zero" in ctx.manifest.cache_stats.note


def test_empty_aggregate_sections_are_marked_per_section(isolated_results):
    """With NO artifacts on disk the lists stay [] -- and say why.

    A row-level `source` cannot mark an empty list; `aggregate_provenance` does.
    """
    ctx = _ctx("agg-3", runner=FakeRunner(crossing=False))
    phases.finalize_manifest(ctx)
    m = ctx.manifest
    assert m.throttle_counters == [] and m.resource_deltas == [] and m.fault_counts == []
    prov = m.aggregate_provenance
    for section in (prov.throttle_counters, prov.resource_deltas,
                    prov.fault_counts, prov.achieved_vs_offered):
        assert section.startswith("not_measured")
    assert m.finalization_error is None


def test_populated_aggregate_sections_name_their_source(isolated_results):
    ctx = _ctx("agg-4", runner=FakeRunner(crossing=False))
    _write_probe_artifacts("agg-4")
    phases.finalize_manifest(ctx)
    prov = ctx.manifest.aggregate_provenance
    assert not prov.throttle_counters.startswith("not_measured")
    assert not prov.resource_deltas.startswith("not_measured")
    assert not prov.fault_counts.startswith("not_measured")
    # the throttle numbers are container-LIFETIME counters, and say so
    assert "LIFETIME" in prov.throttle_counters
    assert ctx.manifest.throttle_counters[0].source == prov.throttle_counters


def test_finalize_manifest_never_raises_on_a_malformed_artifact(isolated_results):
    """A bad artifact must not cost a completed run (it records the failure)."""
    import json as _json

    ctx = _ctx("agg-5", runner=FakeRunner(crossing=False))
    d = paths.run_dir("agg-5") / "cells" / "c" / "ladder" / "rate100" / "block0"
    d.mkdir(parents=True, exist_ok=True)
    (d / "cpu_mem_go.summary.json").write_text(_json.dumps({
        "container": "ledgerline-sut-go-1", "enabled": True,
        "cpu_usage_delta_usec": "not-a-number",
    }))
    phases.finalize_manifest(ctx)                 # no raise
    assert ctx.manifest.finalization_error is not None
    assert "ValueError" in ctx.manifest.finalization_error


def test_cells_phase_is_recorded_done_before_finalization(isolated_results, monkeypatch):
    """Finalization is bookkeeping; it must never decide whether cells are done."""
    ctx = _ctx("agg-6", runner=FakeRunner(crossing=False))
    seen: list[str] = []

    def _boom(_ctx):
        seen.append(ctx.led.phase("cells").state.value)
        raise RuntimeError("malformed artifact")

    monkeypatch.setattr(phases, "_finalize_manifest", _boom)
    phases.run_cells_phase(ctx)
    assert seen == ["done"]                       # already DONE when finalize ran
    assert ctx.led.phase_done("cells") is True
    assert "malformed artifact" in ctx.manifest.finalization_error


# --------------------------------------------------------------------------- #
# F4: the effective cache TTL (the working set's other half) is recorded
# --------------------------------------------------------------------------- #
def test_effective_cache_ttl_falls_back_to_the_compose_default(isolated_results, monkeypatch):
    ctx = _ctx("ttl-1", runner=FakeRunner(crossing=False))
    monkeypatch.delenv("CACHE_TTL_SECONDS", raising=False)
    ttl, source = phases._effective_cache_ttl(ctx)
    # read out of infra/compose.yaml, never assumed
    compose_yaml = (paths.infra_dir() / "compose.yaml").read_text(encoding="utf-8")
    assert f"CACHE_TTL_SECONDS:-{ttl}" in compose_yaml
    assert source.startswith("infra/compose.yaml default")


def test_effective_cache_ttl_ignores_ambient_env_and_prefers_the_lever(isolated_results, monkeypatch):
    # Ambient CACHE_TTL_SECONDS is scrubbed by compose.KNOB_ENV_VARS and never
    # reaches the SUTs, so the manifest must NOT record it: without a lever the
    # source is the compose.yaml default, regardless of the ambient value.
    ctx = _ctx("ttl-2", runner=FakeRunner(crossing=False))
    monkeypatch.setenv("CACHE_TTL_SECONDS", "42")
    ttl, source = phases._effective_cache_ttl(ctx)
    assert ttl == 300 and "compose.yaml default" in source
    ctx.runner.env_overrides = {"CACHE_TTL_SECONDS": "900"}
    ttl, source = phases._effective_cache_ttl(ctx)
    assert ttl == 900 and phases.CACHE_TTL_OVERRIDE_ENV in source


def test_working_set_records_the_ttl_it_ran_with(isolated_results, monkeypatch):
    from ledgerbench import vegeta

    ctx = _ctx("ttl-3", runner=FakeRunner(crossing=False))
    monkeypatch.delenv("CACHE_TTL_SECONDS", raising=False)
    io_dir = paths.run_dir("ttl-3") / "io" / "go"
    vegeta.generate_targets(
        ctx.workload, out_dir=io_dir, count=100,
        seed=ctx.workload.rng.target_generator_seed_default, scale="smoke",
    )
    phases._record_working_set(ctx)
    ws = ctx.manifest.working_set
    assert ws.cache_ttl_seconds is not None       # the TTL-vs-replay comparison works
    assert ws.cache_ttl_source != "not_measured"
    assert ws.distinct_customer_ids_list_urls is not None


# --------------------------------------------------------------------------- #
# identity provenance: the sha a run STARTED at survives a resume
# --------------------------------------------------------------------------- #
def test_resume_keeps_the_starting_git_sha_and_records_the_change(isolated_results, monkeypatch):
    from ledgerbench import manifest as mf

    m = mf.new_manifest("sha-1")
    monkeypatch.setattr(phases, "git_identity", lambda: ("aaaa", False))
    phases._record_git_identity(m)
    assert m.identity.git_sha == "aaaa"

    # resume from a DIFFERENT commit: the original sha must survive
    monkeypatch.setattr(phases, "git_identity", lambda: ("bbbb", True))
    phases._record_git_identity(m)
    assert m.identity.git_sha == "aaaa"
    assert m.identity.git_dirty is False
    assert [e["git_sha"] for e in m.identity.git_sha_changed_during_run] == ["bbbb"]
    phases._record_git_identity(m)                 # same state again: no duplicate
    assert len(m.identity.git_sha_changed_during_run) == 1


def test_exhausted_levers_do_not_change_the_workload(isolated_results, monkeypatch):
    """A gate that fails after every lever must leave the workload at spec.

    Applying levers that did not buy headroom would leave a run that is neither
    the spec workload nor a headroom-clean one.
    """
    runner = GateRunner([_measured_knee(1.0)])
    ctx = _gate_ctx("gate-nolevers", runner, monkeypatch=monkeypatch, allow=True)
    monkeypatch.setenv(phases.CACHE_TTL_OVERRIDE_ENV, "900")
    phases.run_capacity_gate_phase(ctx)
    assert ctx.manifest.capacity_gate.verdict == "db-imposed-shared-knee"
    assert ctx.manifest.capacity_gate.levers_fired            # they were evaluated
    assert ctx.manifest.capacity_gate.levers_applied == []    # but never applied
    assert ctx.workload.mix.create_invoice == 15
    assert runner.env_overrides == {}
    assert ctx.manifest.effective_workload.differs_from_spec is False


def test_cells_refuse_to_start_after_a_failed_gate(isolated_results, monkeypatch):
    """Entering a cell directly (per-stage CLI / resume) re-asserts the gate."""
    runner = GateRunner([_measured_knee(1.0)])
    ctx = _gate_ctx("gate-cell-guard", runner, monkeypatch=monkeypatch, allow=False)
    with pytest.raises(phases.PhaseError, match="db-imposed-shared-knee"):
        phases.run_capacity_gate_phase(ctx)       # degraded, recorded, UNstamped

    ctx2 = _gate_ctx("gate-cell-guard", GateRunner([]), monkeypatch=monkeypatch)
    with pytest.raises(phases.PhaseError, match="db-imposed-shared-knee"):
        phases.run_cell(ctx2, _GC_DEFAULT)
    assert phases._any_cell_probe_done(ctx2.led) is False


def test_cell_entered_directly_gets_the_effective_workload(isolated_results, monkeypatch):
    runner = GateRunner([])
    ctx = _gate_ctx("gate-cell-eff", runner, monkeypatch=monkeypatch)
    runner._knees = [_measured_knee(_needed_knee(ctx, write_share=0.10))]
    phases.run_capacity_gate_phase(ctx)

    runner2 = GateRunner([])
    ctx2 = _gate_ctx("gate-cell-eff", runner2, monkeypatch=monkeypatch)
    monkeypatch.setattr(phases, "IS_LINUX", False)   # skip the pinned-core assert
    phases.run_cell(ctx2, _GC_DEFAULT)
    assert ctx2.workload.mix.create_invoice == 10
    assert runner2.workload.mix.create_invoice == 10
    # every stage re-asserts the gate, but the effective workload is applied ONCE
    # per process: re-applying would regenerate targets on each stage boundary.
    assert runner2.apply_calls == 1


# --------------------------------------------------------------------------- #
# the per-stage CLI entry points (`ledgerbench ladder|confirm|soak`) never call
# run_cell, so each STAGE has to carry the same gate guard.
# --------------------------------------------------------------------------- #
def _failed_gate_run(run_id, monkeypatch):
    """Record a db-imposed-shared-knee verdict in `run_id` WITHOUT opting out.

    The stamped (opted-out) counterpart replays on resume by design; the guard
    the stage entry points must keep is against the UNstamped degraded record.
    """
    ctx = _gate_ctx(run_id, GateRunner([_measured_knee(1.0)]),
                    monkeypatch=monkeypatch, allow=False)
    with pytest.raises(phases.PhaseError, match="db-imposed-shared-knee"):
        phases.run_capacity_gate_phase(ctx)
    assert ctx.led.phase("capacity_gate").data["verdict"] == "db-imposed-shared-knee"
    assert not ctx.led.phase("capacity_gate").data.get("degraded_allowed_by")


@pytest.mark.parametrize("stage_fn", ["run_ladder", "run_fit", "run_confirm", "run_soak"])
def test_every_stage_entry_point_refuses_after_a_failed_gate(
    isolated_results, monkeypatch, stage_fn
):
    _failed_gate_run(f"stage-guard-{stage_fn}", monkeypatch)
    ctx = _gate_ctx(f"stage-guard-{stage_fn}", GateRunner([]), monkeypatch=monkeypatch)
    with pytest.raises(phases.PhaseError, match="db-imposed-shared-knee"):
        getattr(phases, stage_fn)(ctx, _GC_DEFAULT)
    assert phases._any_cell_probe_done(ctx.led) is False


def test_stage_entry_proceeds_when_the_optout_was_stamped(isolated_results, monkeypatch):
    """The stamped opt-out replays at stage entry: a naked resume shell must
    not re-block a degraded-allowed run (only the UNstamped record blocks)."""
    ctx = _gate_ctx("stage-guard-stamped", GateRunner([_measured_knee(1.0)]),
                    monkeypatch=monkeypatch, allow=True)
    phases.run_capacity_gate_phase(ctx)           # degraded, stamped, no raise
    ctx2 = _gate_ctx("stage-guard-stamped", GateRunner([]), monkeypatch=monkeypatch)
    phases.run_ladder(ctx2, _GC_DEFAULT)          # must not raise
    assert ctx2.allow_db_imposed_knee is True
    assert ctx2.led.stage(_GC_DEFAULT.cell_id, "ladder").state == State.DONE


def test_ladder_entered_directly_adopts_the_effective_workload(isolated_results, monkeypatch):
    """`ledgerbench ladder` must run the gate's EFFECTIVE mix, not the spec mix."""
    runner = GateRunner([])
    ctx = _gate_ctx("stage-eff", runner, monkeypatch=monkeypatch)
    runner._knees = [_measured_knee(_needed_knee(ctx, write_share=0.10))]
    phases.run_capacity_gate_phase(ctx)

    runner2 = GateRunner([])
    ctx2 = _gate_ctx("stage-eff", runner2, monkeypatch=monkeypatch)
    monkeypatch.setattr(phases, "IS_LINUX", False)
    assert ctx2.workload.mix.create_invoice == 15      # frozen spec, pre-guard
    phases.run_ladder(ctx2, _GC_DEFAULT)
    assert ctx2.workload.mix.create_invoice == 10
    assert runner2.workload.mix.create_invoice == 10
    assert runner2.knee_calls == []                    # the gate did NOT re-run


@pytest.mark.parametrize("stage_fn", ["run_ladder", "run_confirm", "run_soak"])
def test_measured_stage_refuses_when_no_gate_ever_ran(isolated_results, monkeypatch, stage_fn):
    """An ABSENT verdict is not a passing one (the secondary hole in the guard)."""
    ctx = _gate_ctx(f"stage-nogate-{stage_fn}", GateRunner([]), monkeypatch=monkeypatch)
    assert ctx.led.phase("capacity_gate").data == {}
    with pytest.raises(phases.PhaseError, match="capacity gate has not run"):
        getattr(phases, stage_fn)(ctx, _GC_DEFAULT)
    assert phases._any_cell_probe_done(ctx.led) is False


def test_no_gate_plus_opt_out_runs_but_stamps_degraded(isolated_results, monkeypatch):
    ctx = _gate_ctx("stage-nogate-allow", GateRunner([]), monkeypatch=monkeypatch, allow=True)
    monkeypatch.setattr(phases, "IS_LINUX", False)
    phases.run_ladder(ctx, _GC_DEFAULT)
    assert ctx.led.stage(_GC_DEFAULT.cell_id, "ladder").state == State.DONE
    gate = ctx.manifest.capacity_gate
    assert gate.degraded is True
    assert gate.degraded_allowed_by == phases.ALLOW_DB_IMPOSED_KNEE_ENV
    assert "never established" in gate.detail


def test_smoke_stage_needs_no_gate_record(isolated_results):
    """A functional rehearsal publishes nothing, so it is not gate-blocked."""
    ctx = _ctx("stage-smoke", runner=FakeRunner(crossing=False))   # functional_only
    phases.run_ladder(ctx, _GC_DEFAULT)
    assert ctx.led.stage(_GC_DEFAULT.cell_id, "ladder").state == State.DONE
    assert ctx.manifest.capacity_gate.degraded is False
