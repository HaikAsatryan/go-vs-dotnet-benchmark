"""Phase orchestration + RCB interleaving.

This is the live, ledger-backed, resumable pipeline. Phase order:

    doctor -> host_setup -> seed(+invariants) -> capacity_gate -> validation
      -> cells (ladder -> fit -> confirm -> soak) -> variants -> host_restore

Every phase consults the ledger first and SKIPS work whose state == done, with
the hardwired power-loss exceptions:
  (a) doctor ALWAYS re-runs on process start;
  (b) host_setup ALWAYS re-runs on Linux on process start (a reboot silently
      resets governor/boost/CPUAffinity; the script is idempotent): on Windows
      it is recorded done with data {"skipped": "non-linux"};
  (c) a `done` seed phase, on RESUME, still re-runs the seeder with --verify-only
      (invariant preflight proves PG came back crash-consistent) before any probe.

RCB interleaving: probes are grouped in blocks of {go, dotnet} at
the same rate; the within-block order comes from a seeded RNG (ledger.rng_seed)
and is recorded as `slot_order`. On resume, any block with exactly one member
done is reset whole (Ledger.reset_orphan_blocks) so the paired slot is re-run
clean.

The live execution surface (docker compose up/down, vegeta attack, cgroup
sampling) is isolated behind `ProbeRunner` so the orchestration logic is unit
testable with a fake runner and no daemon. `LiveProbeRunner` is the real one.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol

from . import capacity_gate as capgate
from . import compose, doctor
from .analysis_bridge import estimate_knee_from_rungs, stationarity_fails
from .config import SloConfig, Workload, config_hash, load_slo, load_workload
from .ledger import (
    Ledger,
    ProbeRecord,
    State,
)
from .manifest import NOT_MEASURED, RunManifest, git_identity, new_manifest
from .util import paths
from .util.clock import rfc3339_micros
from .util.pcg64 import PCG64
from .util.proc import run
from .warmup import IMPLEMENTED_GATES

IS_LINUX = sys.platform.startswith("linux")

# The SUT core count pinned in infra/compose.bench.yaml (GOMAXPROCS /
# DOTNET_PROCESSOR_COUNT = ${SUT_CORES:-6}). Kept consistent with that default so
# the manifest knobs and the Linux processor-count assertion agree with compose.
SUT_CORES = int(os.environ.get("SUT_CORES", "6"))


# --------------------------------------------------------------------------- #
# scale profiles
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ScaleProfile:
    """Resolved measurement numerics for a scale.

    `full` reads spec/slo.yaml verbatim. `smoke` is a clearly-labeled
    FUNCTIONAL-REHEARSAL-ONLY override block: tiny windows, few rungs, gc-default
    cell only, so a complete run-all rehearses every code path in ~20 min on the
    dev laptop without producing measurement-grade numbers.
    """

    scale: str
    functional_only: bool
    warmup_cap_s: int
    # Head-of-window discard (slo ladder.warmup_discard_seconds): traffic at the
    # MEASURED rate, run before the measured attack and excluded from the pooled
    # report + cgroup window (guards against residual JIT re-tiering at the
    # measured rate; METHODOLOGY "Warmup").
    warmup_discard_s: int
    measure_s: int
    ladder_start_rps: int
    ladder_step_factor: float
    ladder_max_rungs: int
    ladder_reps_per_rung: int
    confirm_blocks: int
    soak_s: int
    capacity_gate_rungs: int
    capacity_gate_window_s: int
    cells_filter: set[str] | None      # None => all non-linux-only cells
    # attack shaping (kept modest; smoke is functional)
    max_workers: int = 64
    connections: int = 16
    timeout_s: float = 5.0
    target_count: int = 6000
    # Scale on spec/slo.yaml warmup.per_endpoint_min_calls. The spec minimums
    # are full-scale evidence thresholds; the smoke profile (15 s cap) scales
    # them down so the warmup gates are exercised for real instead of being
    # unreachable-by-construction (which would flag EVERY smoke probe
    # under-warmed and empty every rung).
    warmup_min_calls_factor: float = 1.0

    @classmethod
    def resolve(cls, scale: str, slo: SloConfig) -> "ScaleProfile":
        if scale == "full":
            return cls.full(slo)
        if scale == "smoke":
            return cls.smoke(slo)
        raise ValueError(f"unknown scale {scale!r} (expected full|smoke)")

    @classmethod
    def full(cls, slo: SloConfig) -> "ScaleProfile":
        lad = slo.ladder
        soak = slo.soak or {}
        # Bench-side working-set dial: the number of requests in the generated
        # targets file. The default is 1_000_000, what the published run used
        # (137,028 distinct invoices, realized hit rate 0.807). It used to be
        # 20000, which covers only ~4.6k distinct invoices and makes the read path
        # effectively cache-resident: a meaningless full-scale run for anyone who
        # did not read the RUNBOOK first.
        # Bench dial, not spec/: no config_hash change.
        # Targets are generated once per run and reused on resume, so a mid-run
        # env change cannot silently change an existing run's working set.
        raw_targets = os.environ.get(TARGET_COUNT_ENV, "").strip()
        try:
            target_count = int(raw_targets) if raw_targets else 1_000_000
        except ValueError:
            raise ValueError(
                f"{TARGET_COUNT_ENV}={raw_targets!r} is not an integer"
            ) from None
        if target_count < 1000:
            raise ValueError(f"{TARGET_COUNT_ENV}={target_count} is implausibly small (<1000)")
        return cls(
            scale="full",
            functional_only=False,
            warmup_cap_s=slo.warmup.hard_cap_seconds,          # 180
            warmup_discard_s=lad.warmup_discard_seconds,       # 30
            measure_s=lad.window_seconds,                      # 120
            ladder_start_rps=lad.start_rate_rps_default,       # 500
            ladder_step_factor=lad.step_factor,                # 1.2
            ladder_max_rungs=40,
            ladder_reps_per_rung=lad.reps_per_rung,            # 3
            confirm_blocks=lad.confirm_reps,                   # 10
            soak_s=int(soak.get("duration_seconds", 600)),
            capacity_gate_rungs=12,
            capacity_gate_window_s=lad.window_seconds,
            cells_filter=None,
            max_workers=512,
            connections=64,
            timeout_s=float(slo.co_validation.stall_test.get("timeout_s", 5.0))
            if isinstance(slo.co_validation.stall_test, dict) else 5.0,
            target_count=target_count,
        )

    @classmethod
    def smoke(cls, _slo: SloConfig) -> "ScaleProfile":
        # SMOKE_PARAMS: FUNCTIONAL-REHEARSAL-ONLY. Numbers chosen so a full
        # run-all completes in ~20 min on this laptop; NEVER measurement-grade.
        return cls(
            scale="smoke",
            functional_only=True,
            warmup_cap_s=15,
            warmup_discard_s=2,
            measure_s=10,
            ladder_start_rps=100,
            ladder_step_factor=2.0,
            ladder_max_rungs=3,
            ladder_reps_per_rung=2,
            confirm_blocks=2,
            soak_s=20,
            capacity_gate_rungs=2,
            capacity_gate_window_s=10,
            cells_filter={"headline.mixed.gc-default"},
            max_workers=64,
            connections=16,
            timeout_s=5.0,
            target_count=6000,
            warmup_min_calls_factor=0.02,
        )


# --------------------------------------------------------------------------- #
# declarative CELLS table (cells + variants)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Cell:
    cell_id: str
    extra_env: dict[str, str] = field(default_factory=dict)
    linux_only: bool = False
    deferred: bool = False          # declared but recorded "deferred" for now
    note: str = ""


# Headline cells (always runnable) + variants (declared; mostly deferred until
# trivially runnable). gc-generous: Go GOGC=off + a GOMEMLIMIT, .NET DATAS off.
CELLS: list[Cell] = [
    Cell(
        cell_id="headline.mixed.gc-default",
        extra_env={},  # compose.yaml defaults (GOGC=100, DATAS=1)
        note="defaults; the headline baseline.",
    ),
    Cell(
        cell_id="headline.mixed.gc-generous",
        extra_env={
            "GOGC": "off",
            "GOMEMLIMIT": "10GiB",
            "DOTNET_GCDynamicAdaptationMode": "0",
        },
        note="Go GOGC=off+GOMEMLIMIT; .NET DATAS off.",
    ),
    # ---- variants (linux_only; executed via LEDGERBENCH_RUN_VARIANTS) ------ #
    # The scope freeze removed the once-declared quota / boost-on /
    # mem-sweep variants and the isolation cells: they will never run, so the
    # code no longer advertises them. These three are the final variant matrix.
    Cell(
        cell_id="variant.ado",
        extra_env={"DATA_LAYER": "ado"},
        linux_only=True,
        deferred=True,
        note="Raw ADO.NET data layer (.NET-only lever; the no-ORM floor).",
    ),
    Cell(
        cell_id="variant.dapper",
        extra_env={"DATA_LAYER": "dapper"},
        linux_only=True,
        deferred=True,
        note="Dapper data layer (.NET-only lever; the micro-ORM middle point).",
    ),
    Cell(
        cell_id="variant.prepared-parity",
        extra_env={"MAX_AUTO_PREPARE": "20"},
        linux_only=True,
        deferred=True,
        note="Npgsql prepared-statement parity.",
    ),
]

HEADLINE_CELL_IDS = {"headline.mixed.gc-default", "headline.mixed.gc-generous"}


def cells_for(profile: ScaleProfile) -> list[Cell]:
    """Headline cells the active scale should run (variants handled separately)."""
    out: list[Cell] = []
    for c in CELLS:
        if c.cell_id not in HEADLINE_CELL_IDS:
            continue
        if profile.cells_filter is not None and c.cell_id not in profile.cells_filter:
            continue
        out.append(c)
    return out


def variant_cells() -> list[Cell]:
    return [c for c in CELLS if c.cell_id not in HEADLINE_CELL_IDS]


# --------------------------------------------------------------------------- #
# RCB block planning
# --------------------------------------------------------------------------- #
def slot_order_for_block(rng: PCG64) -> list[str]:
    """Seeded within-block {go, dotnet} order (RCB). Consumes one draw."""
    return ["go", "dotnet"] if rng.random() < 0.5 else ["dotnet", "go"]


def plan_blocks(*, n_blocks: int, rate: int, rng_seed: int, salt: int) -> list[ProbeRecord]:
    """Build the probe records for `n_blocks` RCB blocks at one rate.

    The within-block order is seeded by (rng_seed, salt) so it is reproducible
    and auditable in the ledger. `salt` distinguishes stages/rates
    so the gc-default confirm and a soak at the same rate don't share an order
    stream. Each block yields two probes in `slot_order`.
    """
    rng = PCG64(rng_seed ^ (salt * 0x9E3779B97F4A7C15 & ((1 << 64) - 1)))
    probes: list[ProbeRecord] = []
    for block in range(n_blocks):
        order = slot_order_for_block(rng)
        for lang in order:
            probes.append(
                ProbeRecord(block=block, slot_order=list(order), lang=lang, rate=rate)
            )
    return probes


# --------------------------------------------------------------------------- #
# probe execution surface (isolated so orchestration is testable)
# --------------------------------------------------------------------------- #
@dataclass
class ProbeOutcome:
    achieved: float
    error_rate: float
    p99_ms: float
    artifact: str                 # relative path under results/<run_id>/
    per_second_p99: list[float] = field(default_factory=list)
    under_warmed: bool = False
    ok: bool = True
    detail: str = ""


class ProbeRunner(Protocol):
    """How a single warmed probe is executed + its artifacts pulled.

    The live implementation brings up a fresh SUT, warms it, attacks at `rate`
    for `duration_s`, pulls the vegeta JSON summary + per-second p99 CSV + cgroup
    CSVs into the cell/stage dir, checksums them, and returns the outcome. Tests
    pass a deterministic fake.
    """

    def ensure_targets(self, lang: str, cell: Cell) -> None: ...

    def run_probe(
        self,
        *,
        cell: Cell,
        stage: str,
        lang: str,
        rate: int,
        block: int,
        duration_s: int,
        warmup_cap_s: int,
        artifact_dir: Path,
    ) -> ProbeOutcome: ...


# --------------------------------------------------------------------------- #
# run context
# --------------------------------------------------------------------------- #
@dataclass
class RunContext:
    led: Ledger
    profile: ScaleProfile
    workload: Workload          # EFFECTIVE workload (== spec until levers fire)
    slo: SloConfig
    manifest: RunManifest
    runner: ProbeRunner
    run_id: str
    # The frozen spec workload, kept alongside so spec-vs-effective is always
    # derivable (spec/ is never edited; the levers only change run state).
    spec_workload: Workload | None = None
    # Opt-out that lets a non-passing capacity gate proceed, stamping `degraded`
    # into the ledger + manifest. Default False: a failed gate stops the run.
    allow_db_imposed_knee: bool = False
    # Signature of the gate's effective-workload record already applied to THIS
    # process's runner. Lets every stage re-assert the gate (see
    # _ensure_gate_applied) without re-generating targets or recreating
    # containers on each call. None => nothing applied yet in this process.
    gate_applied_signature: str | None = None

    def __post_init__(self) -> None:
        if self.spec_workload is None:
            self.spec_workload = self.workload

    @property
    def slo_ms(self) -> float:
        return float(self.slo.slo.threshold_ms)


# --------------------------------------------------------------------------- #
# phases
# --------------------------------------------------------------------------- #
def run_doctor_phase(ctx: RunContext) -> None:
    """doctor ALWAYS re-runs on process start (power-loss exception a)."""
    report = doctor.run_doctor(require_docker=IS_LINUX)
    out = paths.run_dir(ctx.run_id) / "doctor.json"
    report.save(out)
    failing = [c.name for c in report.checks if not (c.ok or c.skipped)]
    # Docker Desktop (VM engine) voids RESOURCE ground truth, not functionality:
    # a functional-only smoke rehearsal may proceed (recorded), a measured run
    # must not.
    if failing == ["docker_native_engine"] and ctx.profile.functional_only:
        ctx.led.mark_phase(
            "doctor", State.DONE, artifact="doctor.json", all_ok=False,
            waived="docker_native_engine (functional-only scale; no resource truth)",
        )
        return
    state = State.DONE if report.all_ok else State.FAILED
    ctx.led.mark_phase("doctor", state, artifact="doctor.json", all_ok=report.all_ok)
    if not report.all_ok and IS_LINUX:
        raise PhaseError("doctor failed; preconditions not met (see doctor.json)")


def _privileged_argv(argv: list[str]) -> list[str]:
    """Prefix `sudo -n` when not root (host tuning scripts require root).

    The bench box grants the operator NOPASSWD sudo (RUNBOOK prerequisite);
    `-n` makes a missing grant fail loudly at process start instead of hanging
    an unattended resume on a password prompt.
    """
    if os.geteuid() == 0:
        return argv
    return ["sudo", "-n", *argv]


def run_host_setup_phase(ctx: RunContext) -> None:
    """host_setup: ALWAYS re-run on Linux (reboot resets tuning; idempotent).

    On Windows: record done with data {"skipped": "non-linux"} (exception b).
    """
    if not IS_LINUX:
        ctx.led.mark_phase("host_setup", State.DONE, skipped="non-linux", tier="n/a")
        return
    script = paths.infra_dir() / "host-setup.sh"
    res = run(_privileged_argv(["bash", str(script), "--tune"]), check=False, timeout=300)
    state = State.DONE if res.ok else State.FAILED
    # Keep EVERY emitted action line, not a tail: the governor/boost/THP/IRQ/
    # affinity steps are the ones worth recording and a 5-line tail truncated
    # them away in the published run. Nothing reads these back into the
    # manifest (host_state stays source: not_measured); the ledger is where the
    # applied tuning is evidenced.
    ctx.led.mark_phase(
        "host_setup",
        state,
        applied=res.stdout.strip().splitlines(),
        tier="1",
        rc=res.returncode,
    )
    if not res.ok:
        raise PhaseError(f"host-setup.sh --tune failed: {res.stderr.strip()[-300:]}")


def _any_cell_probe_done(led: Ledger) -> bool:
    """True if any cell stage has at least one DONE probe (DB has diverged)."""
    for c in led.cells:
        for st in ("ladder", "fit", "confirm", "soak"):
            if any(p.state == State.DONE for p in getattr(c, st).probes):
                return True
    return False


def _db_has_diverged(led: Ledger) -> tuple[bool, str]:
    """Whether ANY write-probe has hit PG, so exact-count invariants no longer apply.

    The capacity gate's create-only ladder commits real invoices BEFORE any cell
    probe runs; it is marked RUNNING before its first write, so a crash anywhere
    after that point must NOT run the exact-count verify-only preflight (it
    would fail on the legitimately-grown invoice table and abort the resume).
    """
    gate = led.phases.get("capacity_gate")
    if gate is not None and gate.state != State.PENDING:
        return True, "capacity_gate create-only ladder wrote invoices"
    if _any_cell_probe_done(led):
        return True, "cells already wrote to PG (counts diverge by design)"
    return False, ""


def run_seed_phase(ctx: RunContext) -> None:
    """seed + invariants. On RESUME of a done seed, re-run --verify-only first.

    (power-loss exception c): a done seed phase still gets an invariant preflight
    on resume so we never probe against a PG that came back crash-inconsistent,
    UNLESS write-probes have already run (then the seeded counts diverge by
    design and the exact-count invariants no longer apply).
    """
    already_done = ctx.led.phase_done("seed")
    env = compose.ComposeEnv.base(profiles=[compose.PROFILE_SEED])
    dsn = "postgres://postgres:postgres@postgres:5432/ledgerline"
    if already_done:
        # Verify-only invariant preflight (cheap aggregates) proves PG came back
        # crash-consistent (power-loss exception c). BUT the exact-row-count
        # invariants only hold BEFORE any write-probe has run: the capacity
        # gate's create-only ladder and the measured mix (15% create_invoice)
        # both create invoices, so once either has started the DB has
        # LEGITIMATELY diverged from the seeded counts. So we only run the
        # preflight while no write-probe has run; afterward we skip it (the
        # divergence is intended, not corruption) and record why.
        diverged, why = _db_has_diverged(ctx.led)
        if diverged:
            ctx.led.mark_phase("seed", State.DONE, verify_only_skipped=why)
            return
        argv = compose.run_oneshot_argv(
            env, compose.SVC_SEED,
            ["--dsn", dsn, "--scale", ctx.profile.scale, "--seed",
             str(ctx.workload.seeding.seed), "--verify-only"],
        )
        res = compose.compose(env, argv[2:], check=False, timeout=600)
        if not res.ok:
            ctx.led.mark_phase("seed", State.FAILED, verify_only=True, rc=res.returncode)
            raise PhaseError(
                "seed invariant preflight (--verify-only) failed on resume; PG may "
                f"be crash-inconsistent: {(res.stdout + res.stderr).strip()[-300:]}"
            )
        ctx.led.mark_phase("seed", State.DONE, verify_only_on_resume=True)
        return

    # Fresh seed wants a CLEAN DB: the seeder applies spec/schema.sql with bare
    # CREATE TABLE, so a leftover schema (e.g. from a prior run on the same
    # pgdata volume) makes it fail "relation already exists". Drop the volume
    # first on a fresh seed only (resume takes the verify-only branch above and
    # must never wipe).
    reset_db = getattr(ctx.runner, "reset_db", None)
    if callable(reset_db):
        reset_db()

    argv = compose.run_oneshot_argv(
        env, compose.SVC_SEED,
        ["--dsn", dsn, "--scale", ctx.profile.scale, "--seed",
         str(ctx.workload.seeding.seed), "--schema", "/spec/schema.sql",
         "--grants", "--truncate", "--mode", "copy"],
    )
    res = compose.compose(env, argv[2:], check=False, timeout=3600)
    state = State.DONE if res.ok else State.FAILED
    ctx.led.mark_phase("seed", state, scale=ctx.profile.scale, rc=res.returncode)
    if not res.ok:
        raise PhaseError(f"seeder failed: {(res.stdout + res.stderr).strip()[-300:]}")


# Opt-out for a non-passing capacity gate. The DEFAULT is to abort: the
# 2026-06-06 run recorded a db-imposed-shared-knee verdict and published a
# headline anyway because nothing gated on it. Set to 1/true only when the
# degraded run is deliberate; it stamps `degraded` into the ledger AND the
# manifest so every downstream artifact carries the caveat.
ALLOW_DB_IMPOSED_KNEE_ENV = "LEDGERBENCH_ALLOW_DB_IMPOSED_KNEE"
# spec/cache.md realizes the cache_hit_rate lever as a CACHE_TTL_SECONDS raise
# but spec/slo.yaml pre-registers no TTL value; the operator supplies it here
# (spec/ is frozen and no number may be invented).
CACHE_TTL_OVERRIDE_ENV = "LEDGERBENCH_CACHE_TTL_SECONDS"
# Working-set dial: requests in the generated targets file at --scale full
# (default 1_000_000, what the published run used: see docs/RUNBOOK.md).
TARGET_COUNT_ENV = "LEDGERBENCH_TARGET_COUNT"
# Comma-separated variant cell_ids to EXECUTE in the variants phase (default:
# declare-only). Example: LEDGERBENCH_RUN_VARIANTS=variant.ado,variant.dapper
RUN_VARIANTS_ENV = "LEDGERBENCH_RUN_VARIANTS"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _cache_ttl_override() -> int | None:
    raw = os.environ.get(CACHE_TTL_OVERRIDE_ENV, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise PhaseError(
            f"{CACHE_TTL_OVERRIDE_ENV}={raw!r} is not an integer number of seconds"
        ) from None


def _apply_effective_workload(
    ctx: RunContext,
    params: capgate.ResolvedWorkloadParams,
    env_overrides: dict[str, str],
) -> None:
    """Make the resolved lever values REAL for the cells.

    Rewrites ctx.workload into the effective workload and pushes it (plus the
    compose env the topology/TTL levers need) into the runner, which regenerates
    the targets file from the new mix. Without this the levers were computed
    into a throwaway object and every cell kept replaying the frozen spec mix.
    """
    ctx.workload = capgate.apply_params_to_workload(ctx.spec_workload, params)
    apply = getattr(ctx.runner, "apply_effective", None)
    if callable(apply):
        apply(workload=ctx.workload, env_overrides=env_overrides)
    elif env_overrides:
        # A runner that cannot carry the env cannot honor the lever; never let
        # that pass silently as an applied lever.
        raise PhaseError(
            f"capacity-gate levers need compose env {sorted(env_overrides)} but "
            f"{type(ctx.runner).__name__} exposes no apply_effective()"
        )


def _record_gate(
    ctx: RunContext,
    result: capgate.CapacityGateResult,
    *,
    params: capgate.ResolvedWorkloadParams,
    realization: capgate.LeverRealization,
    remeasured: float | None,
    out_dir: Path,
) -> None:
    """Persist the gate outcome to verdict.json + the ledger + the manifest.

    `degraded` is set on ANY non-pass verdict, including a run that aborts here
    without executing a cell: it fails safe (partial artifacts of a stopped run
    must not read as clean). `degraded_allowed_by` is the field that separates
    "stopped" from "ran anyway"; it is non-null only when the operator opt-out
    let the cells proceed.
    """
    from .manifest import CapacityGateStatus

    degraded = result.verdict != capgate.Verdict.PASS
    allowed_by = ALLOW_DB_IMPOSED_KNEE_ENV if (degraded and ctx.allow_db_imposed_knee) else None
    result.levers_applied = list(realization.applied)
    result.levers_not_applied = dict(realization.not_applied)
    result.save(out_dir / "verdict.json")

    effective = capgate.effective_workload_record(
        ctx.spec_workload, ctx.workload, params,
        realization=realization, levers_fired=result.levers_fired,
    )
    ctx.manifest.effective_workload = effective
    ctx.manifest.capacity_gate = CapacityGateStatus(
        verdict=result.verdict.value,
        detail=result.detail,
        pg_knee_rps=result.pg_knee_rps,
        pg_knee_measured=result.pg_knee_measured,
        pg_knee_usable_rungs=result.pg_knee_usable_rungs,
        pg_knee_measured_at_cores=result.pg_knee_measured_at_cores,
        pg_capacity_projected_rps=result.pg_capacity_projected_rps,
        pg_capacity_projection_basis=result.pg_capacity_projection_basis,
        projected_db_rps=result.projected_db_rps,
        headroom_factor=result.headroom_factor,
        levers_fired=[lv.name for lv in result.levers_fired],
        levers_applied=list(realization.applied),
        levers_not_applied=dict(realization.not_applied),
        remeasured_pg_knee_rps=remeasured,
        degraded=degraded,
        degraded_allowed_by=allowed_by,
    )
    ctx.manifest.save()

    ctx.led.mark_phase(
        "capacity_gate", State.DONE,
        verdict=result.verdict.value,
        detail=result.detail,
        # MEASURED knee only; the linear core-scaling projection travels under
        # its own key so the ledger cannot publish an assumption as a reading.
        pg_knee_rps=result.pg_knee_rps,
        pg_knee_measured=result.pg_knee_measured,
        pg_knee_measured_at_cores=result.pg_knee_measured_at_cores,
        pg_knee_usable_rungs=result.pg_knee_usable_rungs,
        pg_capacity_projected_rps=result.pg_capacity_projected_rps,
        pg_capacity_projection_basis=result.pg_capacity_projection_basis,
        remeasured_pg_knee_rps=remeasured,
        levers_fired=[lv.name for lv in result.levers_fired],
        levers_applied=list(realization.applied),
        levers_not_applied=dict(realization.not_applied),
        # Replayed verbatim on resume so the cells run the SAME effective
        # workload after a crash (the gate phase itself is skipped when done).
        effective={
            "write_share": params.write_share,
            "cache_hit_rate": params.cache_hit_rate,
            "pg_cores": params.pg_cores,
            "env_overrides": dict(realization.env_overrides),
        },
        degraded=degraded,
        degraded_allowed_by=allowed_by,
    )


def _enforce_gate(ctx: RunContext, verdict: str, detail: str) -> None:
    """Abort the measured cells on a non-passing gate, unless opted out."""
    if verdict == capgate.Verdict.PASS.value:
        return
    if ctx.profile.functional_only:
        return          # smoke rehearses the path; it publishes nothing
    if ctx.allow_db_imposed_knee:
        return
    raise PhaseError(
        f"capacity gate verdict {verdict!r}: {detail or 'headroom not established'}. "
        "The measured cells would not be a language comparison at a fixed latency "
        f"promise. Set {ALLOW_DB_IMPOSED_KNEE_ENV}=1 to run anyway (the run is "
        "then stamped degraded in the ledger and the manifest)."
    )


def _no_gate_record(ctx: RunContext) -> None:
    """No capacity-gate verdict exists in this run: refuse, or stamp degraded.

    An empty verdict is not a passing one. Without this, entering a stage on a
    run whose gate phase never executed (`ledgerbench ladder/confirm/soak
    --run-id <fresh id>`) measured cells against a DB whose headroom was never
    established; the same hole the gate itself closes, reached by another door.
    """
    if ctx.profile.functional_only:
        return                      # smoke rehearses the path; it publishes nothing
    if not ctx.allow_db_imposed_knee:
        raise PhaseError(
            "the capacity gate has not run in this run (no verdict recorded): a "
            "measured cell may not start before PG's headroom is established. "
            "Run `ledgerbench gate --run-id <id> --scale full` first (`run-all` "
            f"runs it in order), or set {ALLOW_DB_IMPOSED_KNEE_ENV}=1 to measure "
            "ungated (the run is then stamped degraded)."
        )
    # Opted out: the run proceeds, but every downstream artifact carries it.
    gate = ctx.manifest.capacity_gate
    if gate.verdict is None and not gate.degraded:
        gate.degraded = True
        gate.degraded_allowed_by = ALLOW_DB_IMPOSED_KNEE_ENV
        gate.detail = (
            "no capacity gate ran in this run; the cells measured against a DB "
            "whose headroom was never established (operator opt-out)"
        )
        ctx.manifest.save()


def _ensure_gate_applied(ctx: RunContext) -> None:
    """Re-apply a completed gate's effective workload, then re-enforce it.

    Idempotent (the applied state is memoized on the context, so re-asserting it
    per stage costs nothing and never re-triggers a targets regeneration or a
    container recreate) and called from EVERY stage entry point: the gate phase
    is skipped on resume and skipped entirely by the per-stage `ledgerbench
    ladder / confirm / soak` commands, so without this the cells would run
    against the FROZEN spec workload while the manifest claimed the effective
    one, and a degraded verdict would stop blocking after the first crash.
    """
    data = ctx.led.phase("capacity_gate").data
    eff = data.get("effective")
    if isinstance(eff, dict):
        signature = json.dumps(eff, sort_keys=True, default=str)
        if signature != ctx.gate_applied_signature:
            prior_env = dict(getattr(ctx.runner, "env_overrides", {}) or {})
            params = capgate.ResolvedWorkloadParams.from_workload(ctx.spec_workload)
            params.write_share = float(eff.get("write_share", params.write_share))
            params.cache_hit_rate = float(eff.get("cache_hit_rate", params.cache_hit_rate))
            params.pg_cores = int(eff.get("pg_cores", params.pg_cores))
            env_overrides = {str(k): str(v) for k, v in (eff.get("env_overrides") or {}).items()}
            _apply_effective_workload(ctx, params, env_overrides)
            # Recreate PG/vegeta only if THIS process has not already placed them
            # on the lever's cpusets (a per-stage recreate would be pure churn).
            stale_topology = any(prior_env.get(k) != v for k, v in env_overrides.items())
            apply_topology = getattr(ctx.runner, "apply_topology", None)
            if env_overrides and stale_topology and callable(apply_topology):
                apply_topology()
            ctx.gate_applied_signature = signature
    verdict = str(data.get("verdict", ""))
    # The opt-out is RUN state, not shell state: when the completed gate
    # carries the degraded_allowed_by stamp, a resume replays that recorded
    # decision. The documented recovery flow is a naked shell: re-demanding
    # the env var there would re-block a run the operator already chose (and
    # stamped) to continue.
    if data.get("degraded_allowed_by") and not ctx.allow_db_imposed_knee:
        ctx.allow_db_imposed_knee = True
    if not verdict:
        _no_gate_record(ctx)
    elif not verdict.startswith("functional-only"):
        _enforce_gate(ctx, verdict, str(data.get("detail", "")))
        if (ctx.allow_db_imposed_knee and data.get("degraded")
                and not data.get("degraded_allowed_by")):
            # Late opt-in: the gate blocked the original start and the operator
            # resumed WITH the env var. The cells that follow run because of
            # that choice, so stamp it where _record_gate would have (ledger +
            # manifest) without touching the phase's completion timestamp.
            data["degraded_allowed_by"] = ALLOW_DB_IMPOSED_KNEE_ENV
            ctx.led.save()
            gate = ctx.manifest.capacity_gate
            gate.degraded = True
            gate.degraded_allowed_by = ALLOW_DB_IMPOSED_KNEE_ENV
            ctx.manifest.save()


def run_capacity_gate_phase(ctx: RunContext) -> None:
    """Create-only ladder + capacity_gate.evaluate_gate + LEVER APPLICATION.

    On Windows smoke: run the ladder MECHANICS but record verdict
    "functional-only (non-linux)" (no PG cgroup evidence). On Linux: measure
    PG's create-tx ceiling, evaluate the pre-registered levers, APPLY the ones
    that fired (effective workload + compose topology env), re-measure PG when
    the pg_cores lever moved cores, and refuse to continue on a non-pass verdict
    unless the operator opted out.
    """
    if ctx.led.phase_done("capacity_gate"):
        _ensure_gate_applied(ctx)
        return
    out_dir = paths.run_dir(ctx.run_id) / "capacity-gate"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Generate the create-only targets (proves the create path + body sizes).
    # Capped: the gate ladder runs minutes, not hours, and every target carries a
    # ~2 KB create body: LEDGERBENCH_TARGET_COUNT=1000000 would make this one
    # file ~2 GB for no additional gate evidence. 100k ≥ every gate ladder's
    # request budget; below the cap the working-set dial applies unchanged.
    capgate.create_only_targets(
        ctx.workload, out_dir=out_dir, count=min(ctx.profile.target_count, 100_000),
        seed=ctx.workload.rng.target_generator_seed_default, scale=ctx.profile.scale,
    )

    if not IS_LINUX:
        # Functional rehearsal: we still exercised create-only targets gen; the
        # cgroup-evidence-based knee + gate verdict are Linux-only.
        ctx.led.mark_phase(
            "capacity_gate", State.DONE,
            verdict="functional-only (non-linux)",
            note="create-only ladder mechanics exercised; PG cgroup evidence absent.",
            rungs=ctx.profile.capacity_gate_rungs,
        )
        return

    # Linux: drive the create-only ladder against PG, capture cpu.stat + pg_stat,
    # then evaluate the gate over the measured knee (LiveProbeRunner fills
    # the rung table on the measured run).
    # Mark RUNNING BEFORE the first write: a crash mid-gate must already count
    # as DB divergence (see _db_has_diverged) or the resume preflight aborts.
    ctx.led.mark_phase("capacity_gate", State.RUNNING)
    spec_params = capgate.ResolvedWorkloadParams.from_workload(ctx.spec_workload)
    lever_params = capgate.ResolvedWorkloadParams.from_workload(ctx.spec_workload)
    knee = ctx.runner.measure_pg_create_knee(  # type: ignore[attr-defined]
        out_dir=out_dir, rungs=ctx.profile.capacity_gate_rungs,
        window_s=ctx.profile.capacity_gate_window_s,
    )
    empty = capgate.LeverRealization()

    if not knee.measured:
        # No knee => no honest headroom statement. Never fall back to a number.
        result = capgate.unmeasured_knee_result(knee, cfg=ctx.slo.capacity_gate)
        _record_gate(ctx, result, params=spec_params, realization=empty,
                     remeasured=None, out_dir=out_dir)
        _enforce_gate(ctx, result.verdict.value, result.detail)
        return

    projected_knee = float(ctx.profile.ladder_start_rps) * 8.0  # planning proxy
    result = capgate.evaluate_gate(
        pg_knee_rps=knee.knee_rps, projected_knee_rps=projected_knee,
        cfg=ctx.slo.capacity_gate, params=lever_params,
    )
    result.pg_knee_measured = True
    result.pg_knee_usable_rungs = knee.usable_rungs
    result.pg_knee_measured_at_cores = knee.measured_at_cores
    result.pg_knee_is_lower_bound = knee.lower_bound
    result.rungs = knee.rungs

    if result.verdict != capgate.Verdict.PASS:
        # Levers exhausted without reaching headroom. Do NOT apply them: moving
        # the workload off spec buys nothing here and would leave a run that is
        # neither the spec workload nor a headroom-clean one.
        _record_gate(ctx, result, params=spec_params, realization=empty,
                     remeasured=None, out_dir=out_dir)
        _enforce_gate(ctx, result.verdict.value, result.detail)
        return

    realization = capgate.realize_levers(
        result.levers_fired, cache_ttl_seconds=_cache_ttl_override()
    )
    if not realization.fully_applied:
        # Headroom only held because a lever fired that cannot take effect. Apply
        # NOTHING (a partial application matches no evaluated projection) and
        # report which lever is only a claim.
        result.verdict = capgate.Verdict.LEVER_NOT_APPLIED
        result.detail = "; ".join(
            f"{name}: {why}" for name, why in realization.not_applied.items()
        )
        _record_gate(ctx, result, params=spec_params, realization=realization,
                     remeasured=None, out_dir=out_dir)
        _enforce_gate(ctx, result.verdict.value, result.detail)
        return

    applied_params = capgate.params_for_applied_levers(
        spec_params, lever_params, realization.applied
    )
    _apply_effective_workload(ctx, applied_params, realization.env_overrides)

    remeasured: float | None = None
    if realization.env_overrides:
        apply_topology = getattr(ctx.runner, "apply_topology", None)
        if callable(apply_topology):
            topo = apply_topology()
            ctx.led.phase("capacity_gate").data["topology"] = topo
    if "pg_cores" in realization.applied:
        # The linear core-scaling assumption decided WHETHER to spend the lever;
        # it must not reach a published number, so re-measure at the new cores.
        knee2 = ctx.runner.measure_pg_create_knee(  # type: ignore[attr-defined]
            out_dir=out_dir, rungs=ctx.profile.capacity_gate_rungs,
            window_s=ctx.profile.capacity_gate_window_s,
            pg_cores=applied_params.pg_cores,
        )
        if not knee2.measured:
            result.verdict = capgate.Verdict.NO_PG_KNEE_MEASURED
            result.pg_knee_measured = False
            result.detail = f"re-measure after pg_cores lever: {knee2.detail}"
        else:
            remeasured = knee2.knee_rps
            ok, projected = capgate.headroom_after_remeasure(
                remeasured_pg_knee_rps=knee2.knee_rps,
                projected_knee_rps=projected_knee,
                cfg=ctx.slo.capacity_gate, params=applied_params,
            )
            result.projected_db_rps = projected
            if not ok:
                result.verdict = capgate.Verdict.DB_IMPOSED_SHARED_KNEE
                result.detail = (
                    f"re-measured PG knee {knee2.knee_rps:.1f} rps at "
                    f"{applied_params.pg_cores} cores misses the "
                    f"{ctx.slo.capacity_gate.headroom_factor}x headroom over "
                    f"{projected:.1f} rps projected DB load (the linear "
                    "core-scaling projection was optimistic)"
                )

    _record_gate(ctx, result, params=applied_params, realization=realization,
                 remeasured=remeasured, out_dir=out_dir)
    _enforce_gate(ctx, result.verdict.value, result.detail)


def run_validation_phase(ctx: RunContext) -> None:
    """Equivalence pytest (both SUTs up); THE validation gate.

    The once-planned bench-tier checks (CO suite, oha tail cross-check,
    logging-sink microbench, connection sweep) were DESCOPED at scope freeze:
    they never ran and never will, so they are no longer declared. CO safety
    rests on the per-probe achieved-vs-offered evidence plus smoke's stall
    check; the methodology docs carry the descope.
    """
    if ctx.led.phase_done("validation"):
        return
    # Equivalence: invoke the existing pytest the way smoke.py does (both SUTs).
    # On Windows smoke the SUTs may already be torn down; we run it best-effort and
    # record the outcome. The heavy lifting (bringing SUTs up) belongs to the
    # LiveProbeRunner; here we just record the phase from its result.
    eq = ctx.runner.run_equivalence()  # type: ignore[attr-defined]
    ctx.led.mark_phase(
        "validation", State.DONE if eq.get("ok", False) else State.FAILED,
        equivalence=eq,
        note="equivalence is the validation gate; CO/oha/logging-sink/conn-sweep "
             "descoped at scope freeze (see docs/METHODOLOGY.md).",
    )
    if not eq.get("ok", False):
        raise PhaseError(f"equivalence validation failed: {eq.get('detail', '')[:300]}")


# --------------------------------------------------------------------------- #
# cell stages: ladder -> fit -> confirm -> soak
# --------------------------------------------------------------------------- #
def _ladder_rates(profile: ScaleProfile) -> list[int]:
    rates: list[int] = []
    r = float(profile.ladder_start_rps)
    for _ in range(profile.ladder_max_rungs):
        rates.append(int(round(r)))
        r *= profile.ladder_step_factor
    return rates


def _artifact_dir(ctx: RunContext, cell: Cell, stage: str, *extra: str) -> Path:
    d = paths.run_dir(ctx.run_id) / "cells" / cell.cell_id / stage
    for e in extra:
        d = d / e
    d.mkdir(parents=True, exist_ok=True)
    return d


def _record_achieved_offered(
    ctx: RunContext, cell: Cell, stage: str, probe: ProbeRecord, rate: int, ratio: float
) -> None:
    """Append a per-probe achieved-vs-offered row to the manifest.

    Real data only: offered = the ladder/confirm rate, achieved = the vegeta
    report rate. Image digests / host state / cgroup deltas are left for the
    Linux bench-tier (clearly empty rather than fabricated on Windows).
    """
    from .manifest import AchievedVsOffered

    probe_id = f"{cell.cell_id}/{stage}/r{rate}/b{probe.block}/{probe.lang}"
    row = AchievedVsOffered(
        probe_id=probe_id,
        offered_rate=float(rate),
        achieved_rate=float(probe.achieved if probe.achieved is not None else 0.0),
        ratio=round(ratio, 4),
        max_workers=ctx.profile.max_workers,
        connections=ctx.profile.connections,
        timeout_s=ctx.profile.timeout_s,
    )
    # Idempotent on resume: a re-run probe (orphan-block reset / in-flight redo)
    # REPLACES its row instead of appending a duplicate audit record.
    rows = ctx.manifest.achieved_vs_offered
    for i, existing in enumerate(rows):
        if existing.probe_id == probe_id:
            rows[i] = row
            break
    else:
        rows.append(row)
    ctx.manifest.save()


# Which cell env override lands on which language's knob row. GOGC/GOMEMLIMIT are
# Go-only and DOTNET_*/MAX_AUTO_PREPARE/DATA_LAYER are .NET-only: writing a Go
# knob onto the .NET row (as the pre-2026-08 code did for every cell) fabricates
# a value the .NET SUT never saw. Shared knobs apply to both rows.
_GO_ONLY_KNOB_ENV = {"GOGC": "gogc", "GOMEMLIMIT": "gomemlimit"}
_DOTNET_ONLY_KNOB_ENV = {
    "DOTNET_GCDynamicAdaptationMode": "gc_dynamic_adaptation_mode",
    "MAX_AUTO_PREPARE": "max_auto_prepare",
    "DATA_LAYER": "data_layer",
}
_SHARED_KNOB_ENV = {
    "DB_POOL_MAX": "db_pool_max",
    "DB_POOL_MIN": "db_pool_min",
    "REDIS_POOL_MAX": "redis_pool_max",
    "CACHE_TTL_SECONDS": "cache_ttl_seconds",
    "LOG_QUEUE_LEN": "log_queue_len",
}
_INT_KNOBS = {
    "max_auto_prepare", "db_pool_max", "db_pool_min", "redis_pool_max",
    "cache_ttl_seconds", "log_queue_len",
}


def _set_knob(knobs, attr: str, raw: str) -> None:
    setattr(knobs, attr, int(raw) if attr in _INT_KNOBS else raw)


def _record_cell_knobs(ctx: RunContext, cell: Cell) -> None:
    """Echo THIS CELL's knob env overrides + pinned parallelism into the manifest.

    Knobs are recorded PER CELL (manifest.knobs_by_cell): a single run-level pair
    of rows can only describe the last cell that ran, which is what the
    2026-06-06 manifest published (gc-generous values presented as the whole
    run's). Language-specific env lands only on its own language's row.

    Defaults (compose.yaml ${VAR} fallbacks) are not fabricated here. The
    core-parallelism knobs are pinned in compose.bench.yaml, so:
      - gomaxprocs (go) is read from the pinned SUT_CORES env;
      - processor_count is read live from each SUT's /runtime-stats build block.
    On a Linux measured run the reported processor_count MUST equal SUT_CORES
    (compose pins it); a mismatch fails the run loudly. Windows smoke records
    whatever the SUT reports without asserting (no core pinning off-Linux).
    """
    from .manifest import CellKnobs

    env = {**cell.extra_env}
    # Lever-realized env (e.g. CACHE_TTL_SECONDS from the capacity gate) applies
    # to both SUTs and is part of what this cell actually ran with.
    runner_env = getattr(ctx.runner, "env_overrides", None)
    if isinstance(runner_env, dict):
        env.update({k: v for k, v in runner_env.items() if k in _SHARED_KNOB_ENV})

    row = ctx.manifest.knobs_by_cell.get(cell.cell_id) or CellKnobs(cell_id=cell.cell_id)
    row.extra_env = dict(cell.extra_env)
    for var, attr in _GO_ONLY_KNOB_ENV.items():
        if var in env:
            _set_knob(row.go, attr, env[var])
    for var, attr in _DOTNET_ONLY_KNOB_ENV.items():
        if var in env:
            _set_knob(row.dotnet, attr, env[var])
    for var, attr in _SHARED_KNOB_ENV.items():
        if var in env:
            _set_knob(row.go, attr, env[var])
            _set_knob(row.dotnet, attr, env[var])

    # go: GOMAXPROCS is pinned in compose; record it straight from the constant.
    # This one is cell-independent, so it is also the run-level row's value.
    row.go.gomaxprocs = SUT_CORES
    ctx.manifest.knobs_go.gomaxprocs = SUT_CORES

    # processor_count and data_layer are ground-truth from /runtime-stats (the
    # same build block the runtime-stats poller parses). The live runner exposes
    # the read; the fake test runner does not, so this stays best-effort (None
    # off the live path). The data_layer check generalizes an earlier defect:
    # an env override that never reaches the SUT must fail loudly, not record
    # the intended value next to different effective behavior.
    read_build = getattr(ctx.runner, "read_build", None)
    read_pc = getattr(ctx.runner, "read_processor_count", None)
    if callable(read_build) or callable(read_pc):
        expected_dl = {"go": "sqlc", "dotnet": env.get("DATA_LAYER") or "efcore"}
        for lang, cell_knobs, run_knobs in (
            ("go", row.go, ctx.manifest.knobs_go),
            ("dotnet", row.dotnet, ctx.manifest.knobs_dotnet),
        ):
            if callable(read_build):
                build = read_build(lang, extra_env=cell.extra_env)
            else:
                pc_only = read_pc(lang)
                build = None if pc_only is None else SimpleNamespace(
                    processor_count=pc_only, data_layer=None)
            if build is None:
                continue
            pc = build.processor_count
            if pc is not None:
                cell_knobs.processor_count = int(pc)
                run_knobs.processor_count = int(pc)
                if IS_LINUX and int(pc) != SUT_CORES:
                    raise PhaseError(
                        f"{lang} SUT reported processor_count={pc} but the pinned "
                        f"SUT_CORES is {SUT_CORES}; core parallelism is not pinned as "
                        "expected (check compose.bench.yaml + cpuset)"
                    )
            if build.data_layer is not None:
                cell_knobs.data_layer = build.data_layer
                if IS_LINUX and build.data_layer != expected_dl[lang]:
                    raise PhaseError(
                        f"{lang} SUT reported data_layer={build.data_layer!r} but cell "
                        f"{cell.cell_id} expects {expected_dl[lang]!r}; the DATA_LAYER "
                        "env did not reach the SUT (check compose interpolation)"
                    )
    ctx.manifest.knobs_by_cell[cell.cell_id] = row
    ctx.manifest.save()


def _execute_block_probes(
    ctx: RunContext, cell: Cell, stage: str, *, duration_s: int
) -> None:
    """Run all not-done probes in a stage, honoring RCB slot order + resume.

    Block integrity on resume is enforced by the caller (reset_orphan_blocks)
    before this runs. Each probe writes its artifacts + checksum, THEN
    mark_probe_done (probe atomicity).
    """
    rec = ctx.led.stage(cell.cell_id, stage)
    # Group by (block, rate) and execute in the recorded slot order.
    by_block: dict[tuple[int, int], list[ProbeRecord]] = {}
    for p in rec.probes:
        by_block.setdefault((p.block, p.rate), []).append(p)

    for (block, rate) in sorted(by_block.keys()):
        members = by_block[(block, rate)]
        order = members[0].slot_order or ["go", "dotnet"]
        members.sort(key=lambda p: order.index(p.lang) if p.lang in order else 0)
        for probe in members:
            if probe.state == State.DONE:
                continue
            probe.state = State.RUNNING
            ctx.led.save()
            adir = _artifact_dir(ctx, cell, stage, f"rate{rate}", f"block{block}")
            ctx.runner.ensure_targets(probe.lang, cell)
            outcome = ctx.runner.run_probe(
                cell=cell, stage=stage, lang=probe.lang, rate=rate, block=block,
                duration_s=duration_s, warmup_cap_s=ctx.profile.warmup_cap_s,
                artifact_dir=adir,
            )
            if not outcome.ok:
                probe.state = State.FAILED
                ctx.led.save()
                raise PhaseError(
                    f"probe failed: {cell.cell_id}/{stage} block{block} {probe.lang}"
                    f"@{rate}: {outcome.detail[:200]}"
                )
            # record the achieved rate on the probe now so the manifest row reads
            # the real value (mark_probe_done below also sets it; this just makes
            # it available to _record_achieved_offered which runs first).
            probe.achieved = outcome.achieved
            probe_key = f"r{rate}.b{block}.{probe.lang}"
            # Measurement gates (full scale): a probe whose achieved rate fell
            # outside the CO tolerance, or whose error rate exceeded the SLO
            # error_rate_max, is EXCLUDED from the fit input exactly like an
            # under-warmed probe (recorded keyed by probe so resume re-runs
            # overwrite idempotently, never duplicate). Smoke records only.
            ratio = outcome.achieved / rate if rate else 0.0
            tol = ctx.slo.co_validation.achieved_vs_offered_tolerance_pct
            err_max = float(ctx.slo.slo.error_rate_max)
            exclude_reason: str | None = None
            if not ctx.profile.functional_only:
                if abs(ratio - 1.0) * 100.0 > tol:
                    exclude_reason = f"achieved/offered {ratio:.4f} outside +/-{tol}%"
                elif outcome.error_rate > err_max:
                    exclude_reason = f"error_rate {outcome.error_rate:.4f} > {err_max}"
            exc_map = rec.data.setdefault("excluded", {})
            if exclude_reason is not None:
                exc_map[probe_key] = exclude_reason
            else:
                exc_map.pop(probe_key, None)
            rec.data.setdefault("under_warmed", {})[probe_key] = bool(outcome.under_warmed)
            # Pooled per-probe p99 (vegeta report tail) is the SLO ground truth for
            # the knee fit / bracketing. The per-second series is persisted too but
            # feeds ONLY the stationarity / warmup gates.
            rec.data.setdefault("p99_ms", {})[probe_key] = outcome.p99_ms
            if outcome.per_second_p99:
                rec.data.setdefault("p99_series", {})[probe_key] = outcome.per_second_p99
            _record_achieved_offered(ctx, cell, stage, probe, rate, ratio)
            ctx.led.mark_probe_done(
                cell.cell_id, stage, probe,
                artifact=outcome.artifact, achieved=outcome.achieved,
                error_rate=outcome.error_rate,
            )


def run_ladder(ctx: RunContext, cell: Cell) -> None:
    """Geometric ladder until the SLO p99 crossing is bracketed (or cap hit).

    Rungs are planned and executed INCREMENTALLY (rung-major): after each
    rung's RCB blocks complete, the crossing check runs over the rungs measured
    so far; once a rung's mean pooled p99 reaches the SLO the ladder STOPS
    (bracket = [previous rung, crossing rung]) instead of climbing the
    remaining geometric rungs to the cap: at full scale rung 40 is ~616k rps,
    hours of supra-knee timeout storms per cell with zero fit value.

    Resume: already-planned rungs keep their probes (orphan blocks reset
    whole); the crossing check naturally re-derives the stop point from the
    DONE probes, so a resumed ladder never plans past the crossing either.

    NO-CROSSING (smoke scale): record bracket=[top rung], fit data
    {"no_crossing": true}; confirm then runs at the top rung. Probes are RCB
    blocks of reps_per_rung at each rung.

    Re-asserts the capacity gate first: `ledgerbench ladder` enters here without
    ever calling run_cell, so the gate's effective workload and its verdict have
    to be applied at THIS door too.
    """
    stage = "ladder"
    _ensure_gate_applied(ctx)
    rec = ctx.led.stage(cell.cell_id, stage)
    if rec.state == State.DONE:
        return

    if rec.probes:
        ctx.led.reset_orphan_blocks(cell.cell_id, stage)

    for ri, rate in enumerate(_ladder_rates(ctx.profile)):
        # Crossing check over completed rungs BEFORE planning the next one
        # (non-raising: emptied rungs are retried right after execution below).
        by_rate, _ = _rung_values(rec)
        rungs_so_far = sorted((r, sum(v) / len(v)) for r, v in by_rate.items())
        _, crossed = _bracket_crossing(rungs_so_far, ctx.slo_ms)
        if crossed:
            break
        if all(p.rate != rate for p in rec.probes):
            for p in plan_blocks(
                n_blocks=ctx.profile.ladder_reps_per_rung, rate=rate,
                rng_seed=ctx.led.rng_seed, salt=_salt(cell.cell_id, stage, ri),
            ):
                rec.probes.append(p)
            ctx.led.mark_stage(
                cell.cell_id, stage, State.RUNNING,
                rates=sorted({p.rate for p in rec.probes}),
            )
        _execute_block_probes(ctx, cell, stage, duration_s=ctx.profile.measure_s)
        _retry_excluded_rungs(ctx, cell, stage)

    # Build per-rung mean p99 and find the crossing bracket.
    rungs = _collect_rungs(rec)
    bracket, crossed = _bracket_crossing(rungs, ctx.slo_ms)
    ctx.led.mark_stage(
        cell.cell_id, stage, State.DONE,
        bracket=bracket, crossed=crossed,
        rungs_summary={str(r): round(p99, 3) for r, p99 in rungs},
    )


def run_fit(ctx: RunContext, cell: Cell) -> None:
    """Knee fit via analysis.knee + per-probe stationarity (analysis.mann_kendall).

    NO-CROSSING: skip the fit math, set {"no_crossing": true}; confirm uses the
    top rung. With a crossing: feed per-rung p99 reps into estimate_knee
    (respecting min_reps/ci_valid) and run the stationarity gate per probe.

    Gate-guarded like every other stage entry point (`ledgerbench confirm` calls
    run_fit directly): a fit is a published number, so a non-passing gate must
    stop here too.
    """
    stage = "fit"
    _ensure_gate_applied(ctx)
    fit_rec = ctx.led.stage(cell.cell_id, stage)
    if fit_rec.state == State.DONE:
        return
    ladder_rec = ctx.led.stage(cell.cell_id, "ladder")
    bracket = ladder_rec.data.get("bracket") or []
    crossed = bool(ladder_rec.data.get("crossed", False))

    rungs_by_rate = _rungs_by_rate(ladder_rec)
    stat = _stationarity_summary(ctx, ladder_rec)

    if not crossed:
        ctx.led.mark_stage(
            cell.cell_id, stage, State.DONE,
            no_crossing=True, bracket=bracket,
            confirm_rates=bracket or [_top_rate(ladder_rec)],
            stationarity=stat,
            note="no SLO crossing in ladder range (expected at smoke scale); "
                 "fit math skipped, confirm runs at the top rung.",
        )
        return

    knee = estimate_knee_from_rungs(
        rungs_by_rate, slo_ms=ctx.slo_ms,
        min_reps=ctx.slo.ladder.confirm_reps,
    )
    confirm_rates = _confirm_rates_from_bracket(bracket, knee.knee_rps)
    ctx.led.mark_stage(
        cell.cell_id, stage, State.DONE,
        no_crossing=False,
        knee_rps=knee.knee_rps, ci=[knee.ci_low_rps, knee.ci_high_rps],
        ci_valid=knee.ci_valid, warning=knee.warning,
        confirm_rates=confirm_rates, stationarity=stat,
    )


def run_confirm(ctx: RunContext, cell: Cell) -> None:
    """confirm_reps RCB blocks at the bracket rate(s).

    Gate-guarded (the `ledgerbench confirm` command enters here directly).
    """
    stage = "confirm"
    _ensure_gate_applied(ctx)
    rec = ctx.led.stage(cell.cell_id, stage)
    if rec.state == State.DONE:
        return
    fit_rec = ctx.led.stage(cell.cell_id, "fit")
    confirm_rates = [int(r) for r in (fit_rec.data.get("confirm_rates") or [])]
    if not confirm_rates:
        confirm_rates = [_top_rate(ctx.led.stage(cell.cell_id, "ladder"))]

    if not rec.probes:
        for ridx, rate in enumerate(confirm_rates):
            for p in plan_blocks(
                n_blocks=ctx.profile.confirm_blocks, rate=rate,
                rng_seed=ctx.led.rng_seed, salt=_salt(cell.cell_id, stage, ridx),
            ):
                rec.probes.append(p)
        ctx.led.mark_stage(cell.cell_id, stage, State.RUNNING, confirm_rates=confirm_rates)
    else:
        ctx.led.reset_orphan_blocks(cell.cell_id, stage)

    _execute_block_probes(ctx, cell, stage, duration_s=ctx.profile.measure_s)
    for rate in confirm_rates:
        _record_replay_period(ctx, rate)
    ctx.led.mark_stage(cell.cell_id, stage, State.DONE)


def run_soak(ctx: RunContext, cell: Cell) -> None:
    """Soak: one long attack per language at the bracket rate + stationarity.

    Linux additionally records the cgroup anon-slope (the LiveProbeRunner pulls
    mem_anon CSV and computes the slope). Soak is a single RCB block per rate.

    Gate-guarded (the `ledgerbench soak` command enters here directly).
    """
    stage = "soak"
    _ensure_gate_applied(ctx)
    rec = ctx.led.stage(cell.cell_id, stage)
    if rec.state == State.DONE:
        return
    fit_rec = ctx.led.stage(cell.cell_id, "fit")
    rates = [int(r) for r in (fit_rec.data.get("confirm_rates") or [])]
    if not rates:
        rates = [_top_rate(ctx.led.stage(cell.cell_id, "ladder"))]
    soak_rate = rates[0]

    if not rec.probes:
        for p in plan_blocks(
            n_blocks=1, rate=soak_rate,
            rng_seed=ctx.led.rng_seed, salt=_salt(cell.cell_id, stage, 0),
        ):
            rec.probes.append(p)
        ctx.led.mark_stage(cell.cell_id, stage, State.RUNNING, soak_rate=soak_rate)
    else:
        ctx.led.reset_orphan_blocks(cell.cell_id, stage)

    _execute_block_probes(ctx, cell, stage, duration_s=ctx.profile.soak_s)
    _record_replay_period(ctx, soak_rate)

    stat = _stationarity_summary(ctx, rec)
    ctx.led.mark_stage(cell.cell_id, stage, State.DONE, stationarity=stat)


def _reset_rung_probes(led: Ledger, cell_id: str, stage: str, rate: int) -> None:
    """Flip a whole rung back to pending and drop its recorded per-probe data."""
    rec = led.stage(cell_id, stage)
    prefix = f"r{rate}."
    for field_name in ("p99_ms", "under_warmed", "p99_series", "excluded"):
        d = rec.data.get(field_name)
        if isinstance(d, dict):
            for k in [k for k in d if k.startswith(prefix)]:
                d.pop(k)
    for p in rec.probes:
        if p.rate == rate:
            p.state = State.PENDING
            p.artifact = None
            p.achieved = None
            p.error_rate = None
            p.ts = None
    led.save()


def _retry_excluded_rungs(ctx: RunContext, cell: Cell, stage: str) -> None:
    """One-shot retry of any rung emptied by under-warm/gate exclusion.

    Without this, a fully-excluded rung leaves probes DONE in the ledger, the
    fit raises, and every resume raises again on the same baked-in flags: an
    unrecoverable loop. The retry resets the rung whole (pair integrity) and
    re-runs it ONCE (recorded in `rewarmed_rungs` so resumes don't loop);
    emptied again => raise loudly with the remedy.
    """
    rec = ctx.led.stage(cell.cell_id, stage)
    _, empty = _rung_values(rec)
    if not empty:
        return
    retried = {int(r) for r in rec.data.get("rewarmed_rungs", [])}
    for rate in sorted(empty):
        if rate in retried:
            raise PhaseError(
                f"all probes at rung {rate} rps were under-warmed/excluded "
                "after a retry; rerun with a longer warmup cap"
            )
        retried.add(rate)
        rec.data["rewarmed_rungs"] = sorted(retried)
        _reset_rung_probes(ctx.led, cell.cell_id, stage, rate)
        _execute_block_probes(ctx, cell, stage, duration_s=ctx.profile.measure_s)
    _, still_empty = _rung_values(rec)
    if still_empty:
        rate = sorted(still_empty)[0]
        raise PhaseError(
            f"all probes at rung {rate} rps were under-warmed/excluded "
            "after a retry; rerun with a longer warmup cap"
        )


_COMPOSE_CACHE_TTL_RE = re.compile(
    r"CACHE_TTL_SECONDS:\s*\$\{CACHE_TTL_SECONDS:-(\d+)\}"
)


def _effective_cache_ttl(ctx: RunContext) -> tuple[int | None, str]:
    """(seconds, source) for the cache TTL the SUTs actually ran with.

    Precedence mirrors what compose resolves: a capacity-gate lever override on
    the runner, else the default baked into infra/compose.yaml (ambient env is
    scrubbed by compose and therefore never in force). The value is READ, never
    assumed: if the compose default cannot be parsed the TTL stays null with
    `not_measured` rather than acquiring a plausible number.
    """
    lever_env = getattr(ctx.runner, "env_overrides", {}) or {}
    if "CACHE_TTL_SECONDS" in lever_env:
        try:
            return int(lever_env["CACHE_TTL_SECONDS"]), (
                f"capacity-gate cache_hit_rate lever ({CACHE_TTL_OVERRIDE_ENV})"
            )
        except (TypeError, ValueError):
            return None, f"{NOT_MEASURED}: unparseable lever CACHE_TTL_SECONDS"
    # No ambient-environment branch: compose scrubs CACHE_TTL_SECONDS from every
    # invocation (compose.KNOB_ENV_VARS), so an ambient value NEVER reaches the
    # SUTs: recording it here would put a TTL in the manifest the run didn't use.
    try:
        text = (paths.infra_dir() / "compose.yaml").read_text(encoding="utf-8")
    except OSError:
        return None, f"{NOT_MEASURED}: infra/compose.yaml unreadable"
    m = _COMPOSE_CACHE_TTL_RE.search(text)
    if not m:
        return None, f"{NOT_MEASURED}: no CACHE_TTL_SECONDS default in infra/compose.yaml"
    return int(m.group(1)), "infra/compose.yaml default (no override in force)"


def _record_working_set(ctx: RunContext) -> None:
    """Record the DISTINCT keys the generated targets actually touch.

    "5M invoices seeded" is not "5M invoices measured": the targets file is a
    finite list replayed in order, so the read path's real working set is its
    distinct-id count. Recorded once per run (the file is a pure function of
    seed+count+scale+mix, identical for both languages apart from the base URL).

    The effective cache TTL is recorded next to it (with its source): the whole
    point of the section is TTL-versus-replay-period, which is unreadable while
    the TTL is null.
    """
    ws = ctx.manifest.working_set
    if ws.target_count is not None:
        return
    targets = None
    for lang in ("go", "dotnet"):
        candidate = paths.run_dir(ctx.run_id) / "io" / lang / "targets.txt"
        if candidate.exists():
            targets = candidate
            break
    if targets is None:
        return          # not generated yet; recorded after the first probe
    from .manifest import working_set_from_targets

    scale = ctx.workload.seeding.scales.get(ctx.profile.scale)
    new_ws = working_set_from_targets(targets)
    new_ws.targets_file = str(targets.relative_to(paths.run_dir(ctx.run_id)))
    new_ws.seeded_invoices = scale.invoices if scale else None
    new_ws.seeded_customers = scale.customers if scale else None
    new_ws.cache_ttl_seconds, new_ws.cache_ttl_source = _effective_cache_ttl(ctx)
    new_ws.replay_period_seconds = dict(ws.replay_period_seconds)
    ctx.manifest.working_set = new_ws
    ctx.manifest.save()


def _record_replay_period(ctx: RunContext, rate: int) -> None:
    """Seconds to replay the whole targets file at `rate` (F4 disclosure)."""
    ws = ctx.manifest.working_set
    if not ws.target_count or rate <= 0:
        return
    ws.replay_period_seconds[str(rate)] = round(ws.target_count / float(rate), 3)
    ctx.manifest.save()


def run_cell(ctx: RunContext, cell: Cell) -> None:
    """One headline cell: ladder -> fit -> confirm -> soak (each resumable).

    Starts by re-asserting the capacity gate: its effective workload must be in
    force before a single probe runs, and a non-passing verdict must still stop
    the cell even when this cell is entered directly (per-stage CLI entry point
    or a resume that skipped the gate phase). Each stage below re-asserts it too
    (idempotently), so the `ladder`/`confirm`/`soak` commands, which never come
    through here, are guarded as well.
    """
    _ensure_gate_applied(ctx)
    _record_cell_knobs(ctx, cell)
    run_ladder(ctx, cell)
    _record_working_set(ctx)      # targets exist by now (first probe generated them)
    run_fit(ctx, cell)
    run_confirm(ctx, cell)
    run_soak(ctx, cell)


def run_cells_phase(ctx: RunContext) -> None:
    # The phase is DONE once every cell has run: finalization is bookkeeping over
    # artifacts already on disk and must never be what decides whether a
    # completed cells phase is recorded (it cannot raise either, but the order
    # makes the guarantee independent of that).
    cells = cells_for(ctx.profile)
    for cell in cells:
        run_cell(ctx, cell)
    ctx.led.mark_phase("cells", State.DONE, cell_ids=[c.cell_id for c in cells])
    finalize_manifest(ctx)


# --------------------------------------------------------------------------- #
# manifest finalization: aggregate what the probes already wrote to disk
# --------------------------------------------------------------------------- #
# What the throttle rows actually are: cpu.stat's nr_throttled/throttled_usec are
# CUMULATIVE over a container's lifetime and are read once at each window's end,
# so a summed row covers each probe container's whole life (warmup + discard +
# measured window), not the measured windows alone.
THROTTLE_SOURCE = (
    "cpu.stat cumulative nr_throttled/throttled_usec read at each probe "
    "window's end and summed over probes (per-container LIFETIME counters: each "
    "term includes that container's warmup and head-of-window discard, not only "
    "the measured window)"
)
def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def finalize_manifest(ctx: RunContext) -> None:
    """Fill the manifest's aggregate sections; NEVER raise.

    Finalization is bookkeeping over artifacts a completed run already wrote, so
    a malformed artifact must not cost the run: the aggregation is wrapped and
    any failure is recorded in `manifest.finalization_error` (visible, not
    swallowed) instead of propagating out of the cells phase.
    """
    try:
        _finalize_manifest(ctx)
    except Exception as exc:                    # noqa: BLE001 - see docstring
        ctx.manifest.finalization_error = f"{type(exc).__name__}: {exc}"
        try:
            ctx.manifest.save()
        except Exception:                       # noqa: BLE001, S110
            pass                                # nothing left to do but not lose the run


def _finalize_manifest(ctx: RunContext) -> None:
    """Aggregate the per-probe artifacts into the manifest's summary sections.

    Everything here already exists on disk after a run; the 2026-06-06 manifest
    published `throttle_counters: []`, `fault_counts: []`, `resource_deltas: []`
    and `errors: {count: 0}` anyway, which reads as "measured, all zero".
    Sources:
      - cells/**/cpu_mem_<lang>.summary.json -> throttle / resource / fault rows
      - cells/**/<lang>.summary.json         -> vegeta status codes -> errors
      - cells/**/cache_stats_<lang>.json     -> redis keyspace hits/misses
    Sections with no data on disk say so: the row-level `source` stays
    "not_measured", AND the section itself is named in `aggregate_provenance`
    (an empty list has no rows to carry a marker).
    """
    from .manifest import (
        CacheStats, Errors, FaultCounts, ResourceDelta, ThrottleCounters,
    )

    cells_dir = paths.run_dir(ctx.run_id) / "cells"
    per_container: dict[str, dict] = {}
    for path in sorted(cells_dir.rglob("cpu_mem_*.summary.json")):
        doc = _load_json(path)
        if not doc or not doc.get("enabled"):
            continue
        agg = per_container.setdefault(str(doc.get("container", path.stem)), {
            "windows": 0, "cpu": 0, "nr_throttled": 0, "throttled_usec": 0,
            "anon_p99": None, "peak": None, "major": None, "minor": None,
            "fault_windows": 0,
        })
        agg["windows"] += 1
        agg["cpu"] += int(doc.get("cpu_usage_delta_usec") or 0)
        agg["nr_throttled"] += int(doc.get("nr_throttled_end") or 0)
        agg["throttled_usec"] += int(doc.get("throttled_usec_end") or 0)
        for key, field_name in (("anon_p99", "mem_anon_p99"), ("peak", "mem_peak")):
            val = doc.get(field_name)
            if val is not None:
                agg[key] = val if agg[key] is None else max(agg[key], int(val))
        maj, minor = doc.get("major_faults_delta"), doc.get("minor_faults_delta")
        if maj is not None:
            agg["major"] = int(maj) + (agg["major"] or 0)
            agg["fault_windows"] += 1
        if minor is not None:
            agg["minor"] = int(minor) + (agg["minor"] or 0)

    throttles, deltas, faults = [], [], []
    for container, agg in sorted(per_container.items()):
        throttles.append(ThrottleCounters(
            container=container, nr_throttled=agg["nr_throttled"],
            throttled_usec=agg["throttled_usec"], windows=agg["windows"],
            source=THROTTLE_SOURCE,
        ))
        deltas.append(ResourceDelta(
            container=container, cpu_usage_usec_delta=agg["cpu"],
            mem_anon_p99_bytes=agg["anon_p99"], mem_peak_bytes=agg["peak"],
            windows=agg["windows"],
            source="cgroup v2 per-probe windows (summed cpu, max memory)",
        ))
        faults.append(FaultCounts(
            container=container, major_faults=agg["major"], minor_faults=agg["minor"],
            windows=agg["fault_windows"],
            source=("memory.stat pgmajfault/pgfault deltas"
                    if agg["fault_windows"] else NOT_MEASURED),
        ))

    # errors: vegeta per-probe summaries (status codes are the ground truth).
    total_requests = 0
    status_codes: dict[str, int] = {}
    summaries = 0
    for lang in ("go", "dotnet"):
        for path in sorted(cells_dir.rglob(f"{lang}.summary.json")):
            doc = _load_json(path)
            if not doc:
                continue
            summaries += 1
            total_requests += int(doc.get("requests") or 0)
            for code, count in (doc.get("status_codes") or {}).items():
                status_codes[str(code)] = status_codes.get(str(code), 0) + int(count)
    non_2xx = sum(n for code, n in status_codes.items() if not code.startswith("2"))
    if summaries:
        ctx.manifest.errors = Errors(
            count=non_2xx,
            rate=(non_2xx / total_requests) if total_requests else 0.0,
            total_requests=total_requests, status_codes=dict(sorted(status_codes.items())),
            probe_summaries=summaries,
            source="vegeta per-probe report status_codes over all measured windows",
        )

    # cache: redis INFO keyspace deltas per probe window.
    hits = misses = 0
    windows = 0
    for path in sorted(cells_dir.rglob("cache_stats_*.json")):
        doc = _load_json(path)
        if not doc or doc.get("keyspace_hits") is None:
            continue
        hits += int(doc["keyspace_hits"])
        misses += int(doc.get("keyspace_misses") or 0)
        windows += 1
    if windows:
        total = hits + misses
        ctx.manifest.cache_stats = CacheStats(
            keyspace_hits=hits, keyspace_misses=misses,
            hit_rate=(hits / total) if total else None, windows=windows,
            source="redis INFO keyspace_hits/misses deltas over measured windows",
            note=f"target by construction: {ctx.workload.cache_construction.target_hit_rate}",
        )
    else:
        ctx.manifest.cache_stats = CacheStats(
            source=NOT_MEASURED,
            note="no probe recorded a redis INFO sample; NOT a measured zero",
        )

    if throttles:
        ctx.manifest.throttle_counters = throttles
    if deltas:
        ctx.manifest.resource_deltas = deltas
    if faults:
        ctx.manifest.fault_counts = faults

    # Section-level provenance: a list that stayed EMPTY has no row to carry a
    # `source`, so it says here why it is empty rather than reading as a
    # measured nothing.
    no_cgroup = (
        f"{NOT_MEASURED}: no cpu_mem_*.summary.json with resource accounting "
        f"enabled under {cells_dir.name}/"
    )
    prov = ctx.manifest.aggregate_provenance
    prov.throttle_counters = THROTTLE_SOURCE if throttles else no_cgroup
    prov.resource_deltas = (
        "cgroup v2 per-probe windows (summed cpu, max memory)" if deltas else no_cgroup
    )
    if not faults:
        prov.fault_counts = no_cgroup
    elif any(f.major_faults is not None for f in faults):
        prov.fault_counts = "memory.stat pgmajfault/pgfault deltas over the measured windows"
    else:
        prov.fault_counts = f"{NOT_MEASURED}: probe artifacts carry no fault reads"
    prov.achieved_vs_offered = (
        "vegeta achieved rate vs offered rate, one row per probe"
        if ctx.manifest.achieved_vs_offered
        else f"{NOT_MEASURED}: no probe recorded an achieved rate"
    )

    _record_working_set(ctx)
    ctx.manifest.save()


def run_variants_phase(ctx: RunContext) -> None:
    """Variants matrix: declare everything; EXECUTE the requested subset.

    LEDGERBENCH_RUN_VARIANTS=<cell_id,cell_id,...> selects which variant cells
    run this run, through the same run_cell path as the headline cells (full
    ladder -> fit -> confirm -> soak, probe-granular resume). The default is
    declare-only, so a plain run-all never silently spends 15 h per variant.
    The selection is pinned in the ledger on first execution: a resume replays
    the PINNED list, so a changed environment cannot reshape a run in flight.
    """
    if ctx.led.phase_done("variants"):
        return
    requested = _requested_variants(ctx)
    by_id = {c.cell_id: c for c in variant_cells()}
    for cell_id in requested:
        run_cell(ctx, by_id[cell_id])

    declared = {c.cell_id: {"linux_only": c.linux_only,
                            "deferred": c.cell_id not in requested,
                            "env": c.extra_env, "note": c.note}
                for c in variant_cells()}
    ctx.led.mark_phase(
        "variants", State.DONE,
        declared=declared,
        executed=list(requested),
        note=(f"executed {sorted(requested)} via {RUN_VARIANTS_ENV}" if requested
              else "variants declared linux_only + deferred; set "
                   f"{RUN_VARIANTS_ENV} to execute a subset."),
    )


def _requested_variants(ctx: RunContext) -> list[str]:
    """The variant selection, VALIDATED and then pinned on the ledger.

    Probe records for an executing variant land in the ledger under the cell_id,
    so a crashed variant run resumes through run_cell like any headline cell,
    but only if the SELECTION survives the resume. The env var is therefore
    recorded on first read and the recorded value wins from then on. Validation
    happens BEFORE the pin: recording a typo'd selection would re-raise the same
    error on every resume with no way out short of hand-editing ledger.json.
    """
    rec = ctx.led.phase("variants")
    pinned = rec.data.get("requested")
    if pinned is not None:
        raw = os.environ.get(RUN_VARIANTS_ENV, "").strip()
        env_now = [v.strip() for v in raw.split(",") if v.strip()] if raw else []
        if raw and env_now != list(pinned):
            print(
                f"NOTE: run {ctx.run_id} pinned variants={list(pinned)}; "
                f"ignoring the environment's {env_now}.", file=sys.stderr,
            )
        return list(pinned)
    raw = os.environ.get(RUN_VARIANTS_ENV, "").strip()
    requested = [v.strip() for v in raw.split(",") if v.strip()] if raw else []
    by_id = {c.cell_id: c for c in variant_cells()}
    unknown = [v for v in requested if v not in by_id]
    if unknown:
        raise PhaseError(
            f"{RUN_VARIANTS_ENV} names unknown variant cell(s) {unknown}; "
            f"declared: {sorted(by_id)}"
        )
    non_linux = [v for v in requested if by_id[v].linux_only and not IS_LINUX]
    if non_linux:
        raise PhaseError(f"variant cell(s) {non_linux} are linux_only")
    rec.data["requested"] = requested
    ctx.led.save()
    return requested


def run_host_restore_phase(ctx: RunContext) -> None:
    """host_restore: reverse host tuning (Linux). Windows: nothing to restore."""
    if not IS_LINUX:
        ctx.led.mark_phase("host_restore", State.DONE, skipped="non-linux")
        return
    script = paths.infra_dir() / "host-restore.sh"
    res = run(_privileged_argv(["bash", str(script)]), check=False, timeout=300)
    ctx.led.mark_phase(
        "host_restore", State.DONE if res.ok else State.FAILED, rc=res.returncode,
    )


# --------------------------------------------------------------------------- #
# top-level orchestration
# --------------------------------------------------------------------------- #
class PhaseError(RuntimeError):
    """A phase failed loudly (no silent skip)."""


def _record_git_identity(manifest: RunManifest) -> None:
    """Stamp the run's starting commit once; record later changes, never hide them.

    `make_context` runs on every process start, so an unconditional write would
    replace the sha the run STARTED at with the sha of whatever is checked out at
    resume time; a run measured across a code change would look single-commit.
    The first reading wins; any different reading afterwards is appended to
    `identity.git_sha_changed_during_run`.
    """
    ident = manifest.identity
    sha, dirty = git_identity()
    if ident.git_sha is None:
        ident.git_sha = sha
        ident.git_dirty = dirty
        return
    if sha is None or (sha == ident.git_sha and dirty == ident.git_dirty):
        return
    seen = ident.git_sha_changed_during_run
    if seen and seen[-1].get("git_sha") == sha and seen[-1].get("git_dirty") == dirty:
        return                      # already recorded this state
    seen.append({"at": rfc3339_micros(), "git_sha": sha, "git_dirty": dirty})


def make_context(
    *,
    run_id: str,
    scale: str,
    runner: ProbeRunner | None = None,
    allow_db_imposed_knee: bool | None = None,
) -> RunContext:
    """Build the run context: load specs, resolve scale, load-or-create ledger.

    `allow_db_imposed_knee` (default False; overridable by the
    LEDGERBENCH_ALLOW_DB_IMPOSED_KNEE env var so the operator has an opt-out
    without a code change) lets a non-passing capacity gate proceed as a
    DEGRADED run; the choice is stamped into the ledger and the manifest.
    """
    wl = load_workload()
    slo = load_slo()
    profile = ScaleProfile.resolve(scale, slo)
    led = Ledger.load_or_create(
        run_id, config_hash=config_hash(), rng_seed=wl.seeding.seed, scale=scale,
        target_count=profile.target_count,
    )
    # The PINNED working-set size wins on resume: ensure_targets regenerates the
    # targets file whenever the effective count differs from the file's meta, so
    # letting a resume shell's LEDGERBENCH_TARGET_COUNT through would silently
    # swap the working set mid-run.
    if led.target_count is not None and led.target_count != profile.target_count:
        print(
            f"NOTE: run {run_id} pinned target_count={led.target_count}; ignoring "
            f"the environment's {profile.target_count}.", file=sys.stderr,
        )
        from dataclasses import replace as _dc_replace
        profile = _dc_replace(profile, target_count=led.target_count)
    manifest = RunManifest.load(run_id) or new_manifest(run_id, config_hash=led.config_hash)
    manifest.resource_accounting = "enabled" if IS_LINUX else "disabled (non-Linux)"
    # Identity provenance: WHICH COMMIT produced these numbers (the 2026-06-06
    # manifest recorded git_sha: null). Recorded ONCE, at the start of the run:
    # a resume runs from whatever is checked out then, and overwriting the sha
    # would quietly rewrite the provenance of probes already measured. A resume
    # from different code is appended instead, so the change is visible.
    _record_git_identity(manifest)
    # Spec-declared warmup gates the runner does not implement, surfaced at the
    # manifest level as well as per probe.
    manifest.unimplemented_warmup_gates = [
        name for name, enabled in slo.warmup.gates.items()
        if enabled and name not in IMPLEMENTED_GATES
    ]
    manifest.save()
    if runner is None:
        from .live_runner import LiveProbeRunner
        runner = LiveProbeRunner(run_id=run_id, profile=profile, workload=wl, slo=slo)
    allow = (
        _env_flag(ALLOW_DB_IMPOSED_KNEE_ENV)
        if allow_db_imposed_knee is None else bool(allow_db_imposed_knee)
    )
    return RunContext(
        led=led, profile=profile, workload=wl, slo=slo,
        manifest=manifest, runner=runner, run_id=run_id,
        spec_workload=wl, allow_db_imposed_knee=allow,
    )


def run_all(ctx: RunContext) -> None:
    """Execute the full phase pipeline in order, each resumable."""
    # The variant selection is pinned HERE, before any phase runs: not when
    # the variants phase first executes. At full scale that phase starts ~30 h
    # in, and the documented crash recovery is a NAKED-shell
    # `run-all --resume` (no env vars): a late pin would let such a resume
    # reach the variants phase with an empty environment and mark every
    # variant deferred, silently dropping ~15 h cells. Pinning first also
    # fails a typo'd selection in seconds instead of at hour 30.
    _requested_variants(ctx)
    prepare = getattr(ctx.runner, "prepare", None)
    if callable(prepare):
        prepare()
    run_doctor_phase(ctx)
    run_host_setup_phase(ctx)
    run_seed_phase(ctx)
    run_capacity_gate_phase(ctx)
    run_validation_phase(ctx)
    run_cells_phase(ctx)
    run_variants_phase(ctx)
    finalize_manifest(ctx)      # last word: aggregates every artifact on disk
    run_host_restore_phase(ctx)


# --------------------------------------------------------------------------- #
# helpers (rung math, salts, stationarity)
# --------------------------------------------------------------------------- #
def _salt(cell_id: str, stage: str, idx: int) -> int:
    h = 1469598103934665603
    for ch in f"{cell_id}|{stage}|{idx}":
        h = (h ^ ord(ch)) * 1099511628211 & ((1 << 64) - 1)
    return h


def _rung_values(rec) -> tuple[dict[int, list[float]], set[int]]:
    """(measurement-grade p99 values by rate, rates emptied by exclusion).

    Excluded from the fit input: under-warmed probes (warmup hard-capped before
    the gates settled) and gate-excluded probes (`excluded` map: error rate over
    slo error_rate_max, or achieved-vs-offered outside the CO tolerance: both
    full-scale only). Never raises; callers decide how to treat empty rungs
    (the ladder retries them once, the fit refuses to fit around the hole).
    """
    p99_by_probe = rec.data.get("p99_ms", {})
    under_warmed = rec.data.get("under_warmed", {})
    excluded = rec.data.get("excluded", {})
    seen_rates: set[int] = set()
    by_rate: dict[int, list[float]] = {}
    for key, p99 in p99_by_probe.items():
        # key = r<rate>.b<block>.<lang>
        try:
            rate = int(key.split(".")[0][1:])
        except (ValueError, IndexError):
            continue
        seen_rates.add(rate)
        if under_warmed.get(key) or key in excluded:
            continue
        by_rate.setdefault(rate, []).append(float(p99))
    return by_rate, {r for r in seen_rates if r not in by_rate}


def _rungs_by_rate(rec) -> dict[int, list[float]]:
    """Per-rung list of per-probe pooled p99 (ms) for the SLO crossing + fit.

    The SLO is defined on vegeta's POOLED report p99 (one number per probe), so
    that is what feeds the isotonic fit and rate bracketing. The per-second p99
    series is NOT used here (it backs the stationarity/warmup gates only).

    A rung observed but emptied entirely by exclusion is a hole: raise rather
    than silently fit around it (the ladder's one-shot retry has already run by
    the time the fit consumes this).
    """
    by_rate, empty = _rung_values(rec)
    if empty:
        rate = sorted(empty)[0]
        raise PhaseError(
            f"all probes at rung {rate} rps were under-warmed/excluded; "
            "cannot fit around the hole (rerun the rung with a longer warmup cap)"
        )
    return by_rate


def _collect_rungs(rec) -> list[tuple[int, float]]:
    """Mean pooled-p99 per rung (warm probes only). Ascending (rate, mean_p99_ms).

    Drives the SLO crossing bracket. Same pooled per-probe p99 the fit consumes,
    averaged across the warm reps at each rung.
    """
    by_rate = _rungs_by_rate(rec)
    if not by_rate:
        # nothing measured (e.g. no probe done yet) -> empty
        return []
    out = [(rate, sum(v) / len(v)) for rate, v in by_rate.items()]
    out.sort(key=lambda t: t[0])
    return out


def _bracket_crossing(rungs: list[tuple[int, float]], slo_ms: float) -> tuple[list[int], bool]:
    """Bracket the first rung where mean p99 crosses the SLO. No crossing -> top."""
    if not rungs:
        return [], False
    for i, (rate, p99) in enumerate(rungs):
        if p99 >= slo_ms:
            if i == 0:
                return [rate], True
            return [rungs[i - 1][0], rate], True
    return [rungs[-1][0]], False


def _top_rate(rec) -> int:
    rungs = _collect_rungs(rec)
    if rungs:
        return rungs[-1][0]
    rates = rec.data.get("rates") or []
    return int(rates[-1]) if rates else 0


def _confirm_rates_from_bracket(bracket: list[int], knee_rps: float) -> list[int]:
    if bracket:
        return [int(b) for b in bracket]
    return [int(round(knee_rps))]


def _stationarity_summary(ctx: RunContext, rec) -> dict:
    """Per-probe stationarity gate over each probe's per-second p99 (analysis)."""
    series = rec.data.get("p99_series", {})
    min_slope = (ctx.slo.stationarity or {}).get("min_slope_ms_per_min", 1.0) \
        if ctx.slo.stationarity else 1.0
    results: dict[str, bool] = {}
    for key, vals in series.items():
        if len(vals) >= 3:
            results[key] = stationarity_fails(vals, min_slope_ms_per_min=float(min_slope))
    any_fail = any(results.values())
    return {"per_probe_fails": results, "any_fail": any_fail,
            "min_slope_ms_per_min": float(min_slope)}
