"""DB capacity gate (spec/slo.yaml `capacity_gate`).

Runs FIRST among measured phases. Drives a create-only vegeta ladder
against PG in bench topology, captures PG-container cpu.stat delta + pg_stat
sampling per rung, finds PG's create-tx ceiling on 3 cores, and applies the
pre-registered levers in order if headroom is short.

This module holds:
  - the create-only targets generation (reuses vegeta.generate_targets with a
    create-only mix),
  - the lever-logic state machine (pure, tested) over the spec lever list,
  - the headroom rule + verdict model,
  - the knee-from-rungs rule (a "knee" needs >= MIN_PG_KNEE_RUNGS usable rungs;
    fewer is `no usable PG knee measured`, never a number),
  - the LEVER REALIZATION step: what each fired lever actually changes in the
    run (effective workload mix / cache TTL / PG cpuset), so a lever that only
    fired on paper can never be mistaken for a lever that took effect,
  - the pg_stat sampler (the SQL it issues).
The runner drives the attack/sampling execution on the measured run.

spec/ is FROZEN (the ledger's config_hash covers the whole tree), so the levers
NEVER edit workload.yaml: they produce an EFFECTIVE workload held in run state
and echoed in the ledger + manifest next to the frozen spec hash.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from .config import CapacityGate as CapacityGateCfg
from .config import Workload
from .manifest import EffectiveWorkload
from .util import paths
from .vegeta import GeneratedTargets, generate_targets


class Verdict(str, Enum):
    PASS = "pass"
    DB_IMPOSED_SHARED_KNEE = "db-imposed-shared-knee"
    # The create-only ladder did not yield enough usable rungs to call anything
    # a knee (see pg_knee_from_rungs). Distinct from DB_IMPOSED: we do not know
    # PG's ceiling at all, so no verdict about headroom can be honest.
    NO_PG_KNEE_MEASURED = "no-pg-knee-measured"
    # Headroom only holds because a lever fired that cannot be mechanically
    # applied to this run (e.g. cache_hit_rate with no CACHE_TTL_SECONDS given).
    LEVER_NOT_APPLIED = "lever-not-applied"


# Read-only pg_stat probes the gate issues per rung. Kept
# as literal SQL so the runner executes them verbatim; no writes.
PG_STAT_QUERIES: dict[str, str] = {
    "wait_events": (
        "SELECT wait_event_type, wait_event, count(*) AS n "
        "FROM pg_stat_activity WHERE state = 'active' "
        "GROUP BY 1, 2 ORDER BY n DESC"
    ),
    "bgwriter": "SELECT * FROM pg_stat_bgwriter",
    "checkpointer": "SELECT * FROM pg_stat_checkpointer",
    "locks": (
        "SELECT mode, count(*) AS n FROM pg_locks "
        "WHERE NOT granted GROUP BY 1 ORDER BY n DESC"
    ),
}


@dataclass
class RungResult:
    rate: int
    achieved: float
    p99_ms: float
    error_rate: float
    pg_cpu_delta_usec: int | None = None
    pg_stat: dict = field(default_factory=dict)


class LeverApplication(BaseModel):
    name: str
    from_value: float | int | None = None
    to_value: float | int | None = None
    taken_from: str | None = None


_BENCH_PG_CORES = 3  # bench topology default; the measured baseline.

# A create-only ladder that breaks on its first rung has measured ONE point, not
# a knee. Below this many usable rungs the gate reports "no usable PG knee
# measured" instead of a number that reads like a measurement.
MIN_PG_KNEE_RUNGS = 3
# A rung is USABLE if the load generator actually delivered the offered rate and
# PG answered cleanly; anything else says nothing about PG's ceiling.
RUNG_TRACKING_TOLERANCE = 0.95      # achieved >= 0.95 * offered
RUNG_ERROR_MAX = 0.01               # error rate <= 1%


@dataclass
class PgKneeMeasurement:
    """Outcome of the create-only ladder. `knee_rps is None` => NOT measured.

    `usable_rungs` counts rungs where the generator tracked the offered rate and
    errors stayed under RUNG_ERROR_MAX. `lower_bound` marks a ladder that ran out
    of rungs while still tracking (PG's ceiling is above the top rung, so the
    number is a floor, not a knee).
    """

    knee_rps: float | None
    usable_rungs: int
    total_rungs: int
    measured_at_cores: int
    detail: str
    lower_bound: bool = False
    rungs: list[dict] = field(default_factory=list)

    @property
    def measured(self) -> bool:
        return self.knee_rps is not None


def pg_knee_from_rungs(
    rungs: list[RungResult],
    *,
    measured_at_cores: int = _BENCH_PG_CORES,
    min_rungs: int = MIN_PG_KNEE_RUNGS,
) -> PgKneeMeasurement:
    """Derive PG's create-tx ceiling from the create-only ladder, or refuse.

    Pure (unit-tested). The knee is the highest ACHIEVED rate over the usable
    rungs; usability is the same open-loop honesty rule the mixed ladder uses
    (achieved tracks offered, errors under the cap). With fewer than `min_rungs`
    usable rungs we return knee_rps=None: one or two points cannot distinguish
    "PG's ceiling" from "the first rung happened to fail", and the 2026-06-06 run
    published exactly that (a single 500 rps rung reported as a 666.67 rps knee).
    """
    usable = [
        r for r in rungs
        if r.achieved >= r.rate * RUNG_TRACKING_TOLERANCE and r.error_rate <= RUNG_ERROR_MAX
    ]
    usable_ids = {id(r) for r in usable}   # identity, not value: rungs can tie
    as_dicts = [
        {"rate": r.rate, "achieved": r.achieved, "p99_ms": r.p99_ms,
         "error_rate": r.error_rate, "pg_cpu_delta_usec": r.pg_cpu_delta_usec,
         "usable": id(r) in usable_ids}
        for r in rungs
    ]
    if len(usable) < min_rungs:
        return PgKneeMeasurement(
            knee_rps=None, usable_rungs=len(usable), total_rungs=len(rungs),
            measured_at_cores=measured_at_cores, rungs=as_dicts,
            detail=(
                f"no usable PG knee measured: {len(usable)} usable rung(s) of "
                f"{len(rungs)} attempted, need >= {min_rungs} (a knee needs a "
                f"ladder, not a point)"
            ),
        )
    top_usable = max(usable, key=lambda r: r.achieved)
    lower_bound = len(usable) == len(rungs)
    return PgKneeMeasurement(
        knee_rps=top_usable.achieved,
        usable_rungs=len(usable),
        total_rungs=len(rungs),
        measured_at_cores=measured_at_cores,
        rungs=as_dicts,
        lower_bound=lower_bound,
        detail=(
            "ladder never broke (every rung tracked): knee is a LOWER BOUND, "
            "not a ceiling" if lower_bound else "knee = top usable rung"
        ),
    )


@dataclass
class ResolvedWorkloadParams:
    """In-memory, mutable copy of the gate-tunable workload parameters.

    The levers edit THIS, not workload.yaml on disk; each change is echoed in the
    manifest. write_share starts at the create_invoice mix share. The non-write
    endpoint fractions are carried verbatim from workload.yaml so the DB-load
    projection is computed from the ACTUAL per-endpoint mix and each endpoint's
    real DB-touch behavior, NOT from a (1 - write_share) read-share complement
    (healthz and pricing_quote never touch PG, so the read mix is not the
    complement of writes).
    """

    write_share: float          # create_invoice fraction (the write lever target)
    cache_hit_rate: float
    pg_cores: int
    # Non-write endpoint fractions (of total offered rps), held fixed across the
    # write_share lever. DB-touch behavior is encoded in `projected_db_load_rps`.
    get_invoice_share: float
    list_invoices_share: float
    pdf_stub_share: float
    pricing_quote_share: float
    healthz_share: float

    @classmethod
    def from_workload(cls, wl: Workload) -> "ResolvedWorkloadParams":
        m = wl.mix
        return cls(
            write_share=m.create_invoice / 100.0,
            cache_hit_rate=wl.cache_construction.target_hit_rate,
            pg_cores=_BENCH_PG_CORES,
            get_invoice_share=m.get_invoice / 100.0,
            list_invoices_share=m.list_invoices / 100.0,
            pdf_stub_share=m.pdf_stub / 100.0,
            pricing_quote_share=m.pricing_quote / 100.0,
            healthz_share=m.healthz / 100.0,
        )


def projected_db_load_rps(knee_rps: float, params: ResolvedWorkloadParams) -> float:
    """Projected SUT-knee DB load.

    Sum of per-endpoint DB-touch contributions over the ACTUAL mix, NOT a
    (1 - write_share) complement. Per-endpoint DB-touch behavior:
      - create_invoice (write_share): ALWAYS hits PG (insert tx).
      - list_invoices:                ALWAYS hits PG (paged query).
      - pdf_stub:                     ALWAYS hits PG (reads the invoice header).
      - get_invoice:                  hits PG only on cache MISS = (1 - hit_rate).
      - pricing_quote:                NEVER touches PG (pure compute).
      - healthz:                      NEVER touches PG.

    So changing write_share moves only the create term; the read DB-touch terms
    (list, pdf, get-on-miss) keep their own mix weights. cache_hit_rate scales
    only the get_invoice term. The exact split is refined with the gate's measured
    hit rate; this is the pre-registered projection.
    """
    miss = 1.0 - params.cache_hit_rate
    db_touch_fraction = (
        params.write_share                          # create: always
        + params.list_invoices_share                # list:   always
        + params.pdf_stub_share                     # pdf:    always (header read)
        + params.get_invoice_share * miss           # get:    only on cache miss
        # pricing_quote + healthz: never -> omitted
    )
    return knee_rps * db_touch_fraction


def headroom_ok(pg_capacity_rps: float, projected_db_rps: float, headroom_factor: float) -> bool:
    """PG capacity must be >= projected DB load * headroom_factor."""
    return pg_capacity_rps >= projected_db_rps * headroom_factor


def effective_pg_capacity_rps(measured_pg_knee_rps: float, params: ResolvedWorkloadParams) -> float:
    """PG capacity after the pg_cores lever, scaled from the measured 3-core knee.

    PRE-REGISTERED APPROXIMATION: capacity scales LINEARLY with core count,
    capacity = measured_3core_knee * pg_cores / 3. Linear core scaling is the
    pinned planning assumption used only to decide WHETHER to apply the
    `pg_cores` lever in the dry projection; the real gate RE-MEASURES PG at the
    new core count and never trusts this number for the headline. `measured_pg_knee_rps`
    is the create-tx ceiling measured at the bench-default 3 cores.
    """
    return measured_pg_knee_rps * (params.pg_cores / _BENCH_PG_CORES)


def projection_basis(measured_pg_knee_rps: float, params: ResolvedWorkloadParams) -> str:
    """Human-readable arithmetic behind `effective_pg_capacity_rps`.

    Published next to the projected number so a reader never has to guess
    whether it was measured: when the pg_cores lever did not move cores the
    projection IS the measurement, and when it did the string names the factor.
    """
    if params.pg_cores == _BENCH_PG_CORES:
        return (
            f"no scaling applied: pg_cores unchanged at {_BENCH_PG_CORES}, so "
            f"the projection equals the measured knee"
        )
    return (
        f"measured {measured_pg_knee_rps:.4f} rps at {_BENCH_PG_CORES} cores x "
        f"{params.pg_cores}/{_BENCH_PG_CORES} = "
        f"{effective_pg_capacity_rps(measured_pg_knee_rps, params):.4f} rps "
        "under the LINEAR core-scaling assumption. This is a projection, NOT a "
        "measurement: the gate re-measures PG at the new core count before any "
        "number reaches the report"
    )


class LeverMachine:
    """Applies the pre-registered levers in order until headroom holds.

    Pure state machine over spec/slo.yaml capacity_gate.levers_in_order. Each
    `try_next` returns the applied lever (or None if exhausted) and mutates the
    resolved params. NEVER touches synchronous_commit (spec `never`).
    """

    def __init__(self, cfg: CapacityGateCfg, params: ResolvedWorkloadParams):
        self.cfg = cfg
        self.params = params
        self._index = 0
        self.applied: list[LeverApplication] = []

    def try_next(self) -> LeverApplication | None:
        while self._index < len(self.cfg.levers_in_order):
            lever = self.cfg.levers_in_order[self._index]
            self._index += 1
            if lever.name == "synchronous_commit_off" or lever.name == self.cfg.never:
                continue  # forbidden; skip defensively
            app = self._apply(lever.name, lever.from_, lever.to, lever.taken_from)
            if app is not None:
                self.applied.append(app)
                return app
        return None

    def _apply(self, name: str, frm, to, taken_from) -> LeverApplication | None:
        if name == "write_share":
            self.params.write_share = float(to)
            return LeverApplication(name=name, from_value=frm, to_value=to)
        if name == "cache_hit_rate":
            self.params.cache_hit_rate = float(to)
            return LeverApplication(name=name, from_value=frm, to_value=to)
        if name == "pg_cores":
            self.params.pg_cores = int(to)
            return LeverApplication(name=name, from_value=frm, to_value=to, taken_from=taken_from)
        return None


class CapacityGateResult(BaseModel):
    """The gate's decision plus everything the decision rested on.

    `pg_knee_rps` is the MEASURED create-only knee at `pg_knee_measured_at_cores`
    and is never scaled. The linear core-scaling number the lever logic compares
    against the projected DB load lives in `pg_capacity_projected_rps`, with the
    arithmetic spelled out in `pg_capacity_projection_basis`. Keeping them apart
    is F1: the 2026-06-06 verdict.json published `pg_knee_rps: 666.674`, which
    was a measured 500.0057 rps multiplied by 4/3; a projection wearing the name
    of a measurement.
    """

    verdict: Verdict
    pg_knee_rps: float | None = None          # MEASURED (never scaled)
    pg_capacity_projected_rps: float | None = None   # linear core-scaling ASSUMPTION
    pg_capacity_projection_basis: str = ""
    projected_db_rps: float | None = None
    headroom_factor: float
    levers_fired: list[LeverApplication] = Field(default_factory=list)
    rungs: list[dict] = Field(default_factory=list)
    # Provenance of the knee the verdict rests on (F1: a one-rung "knee" must be
    # visible as such, and an unmeasured knee must not read as zero headroom).
    pg_knee_measured: bool = True
    pg_knee_usable_rungs: int | None = None
    pg_knee_measured_at_cores: int | None = None
    pg_knee_is_lower_bound: bool = False
    detail: str = ""
    # Filled by the runner once the fired levers are realized against the run
    # (empty while the evaluation is still a dry projection).
    levers_applied: list[str] = Field(default_factory=list)
    levers_not_applied: dict[str, str] = Field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.verdict == Verdict.PASS

    def save(self, path: Path) -> None:
        paths.write_text_atomic(path, self.model_dump_json(indent=2))


def unmeasured_knee_result(
    knee: PgKneeMeasurement, *, cfg: CapacityGateCfg
) -> CapacityGateResult:
    """Verdict for a create-only ladder that never produced a knee."""
    return CapacityGateResult(
        verdict=Verdict.NO_PG_KNEE_MEASURED,
        pg_knee_rps=None,
        projected_db_rps=None,
        headroom_factor=cfg.headroom_factor,
        rungs=knee.rungs,
        pg_knee_measured=False,
        pg_knee_usable_rungs=knee.usable_rungs,
        pg_knee_measured_at_cores=knee.measured_at_cores,
        detail=knee.detail,
    )


def evaluate_gate(
    *,
    pg_knee_rps: float,
    projected_knee_rps: float,
    cfg: CapacityGateCfg,
    params: ResolvedWorkloadParams,
) -> CapacityGateResult:
    """Apply levers in order until headroom holds or they're exhausted.

    `pg_knee_rps` is PG's measured create-tx ceiling AT THE BENCH-DEFAULT 3 CORES;
    `projected_knee_rps` is the expected mixed-workload knee used to project DB
    load. The pg_cores lever scales the effective capacity linearly (see
    `effective_pg_capacity_rps`), so a case that only passes with 4 cores is
    correctly recognised instead of being mislabeled DB-imposed. Pure (no I/O) so
    the decision is unit-tested; the runner feeds in measured values.
    """
    machine = LeverMachine(cfg, params)

    def _result(verdict: Verdict, projected: float, capacity: float) -> CapacityGateResult:
        return CapacityGateResult(
            verdict=verdict,
            # MEASURED knee in, measured knee out: the scaled number gets its own
            # field so no reader can mistake the projection for a measurement.
            pg_knee_rps=pg_knee_rps,
            pg_capacity_projected_rps=capacity,
            pg_capacity_projection_basis=projection_basis(pg_knee_rps, params),
            projected_db_rps=projected,
            headroom_factor=cfg.headroom_factor,
            pg_knee_measured_at_cores=_BENCH_PG_CORES,
            levers_fired=machine.applied,
        )

    projected = projected_db_load_rps(projected_knee_rps, params)
    capacity = effective_pg_capacity_rps(pg_knee_rps, params)
    if headroom_ok(capacity, projected, cfg.headroom_factor):
        return _result(Verdict.PASS, projected, capacity)
    while True:
        lever = machine.try_next()
        if lever is None:
            break
        # Re-project load and re-scale capacity after EACH lever (pg_cores changes
        # capacity; write_share/cache_hit_rate change the projection).
        projected = projected_db_load_rps(projected_knee_rps, params)
        capacity = effective_pg_capacity_rps(pg_knee_rps, params)
        if headroom_ok(capacity, projected, cfg.headroom_factor):
            return _result(Verdict.PASS, projected, capacity)
    # Exhausted: honest fallback.
    return _result(Verdict.DB_IMPOSED_SHARED_KNEE, projected, capacity)


def headroom_after_remeasure(
    *,
    remeasured_pg_knee_rps: float,
    projected_knee_rps: float,
    cfg: CapacityGateCfg,
    params: ResolvedWorkloadParams,
) -> tuple[bool, float]:
    """Headroom check against a knee RE-MEASURED at the EFFECTIVE core count.

    `effective_pg_capacity_rps` scales the 3-core knee linearly to decide WHETHER
    to spend the pg_cores lever; that linear assumption must never reach a
    published number. Once the cores are actually moved the runner re-measures
    and calls this: no scaling, measured capacity vs projected load.
    Returns (ok, projected_db_rps).
    """
    projected = projected_db_load_rps(projected_knee_rps, params)
    return headroom_ok(remeasured_pg_knee_rps, projected, cfg.headroom_factor), projected


# --------------------------------------------------------------------------- #
# lever REALIZATION: turning a fired lever into a change the cells actually run
# --------------------------------------------------------------------------- #
# The write_share lever removes create traffic from the mix. The removed share
# has to land somewhere (the mix must still sum to 100), and the pre-registered
# choice is the one endpoint whose DB-touch contribution is ZERO, so the
# projection in `projected_db_load_rps` stays exactly true after the rewrite:
# pricing_quote is pure compute (spec/db-access.md: never touches PG).
WRITE_SHARE_ABSORBER = "pricing_quote"

# infra/compose.bench.yaml cpuset defaults; the pg_cores lever moves cores
# between these two sets (spec lever 3: taken_from: vegeta).
COMPOSE_CPUSET_DEFAULTS = {"PG_CPUS": "8-10", "VEGETA_CPUS": "11-13"}
_DONOR_ENV_BY_NAME = {"vegeta": "VEGETA_CPUS"}


def parse_cpuset(spec: str) -> list[int]:
    """`"8-10,14"` -> [8, 9, 10, 14] (docker cpuset syntax, ascending, unique)."""
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.update(range(int(lo), int(hi) + 1))
        else:
            out.add(int(part))
    return sorted(out)


def format_cpuset(ids: list[int]) -> str:
    """[8, 9, 10, 14] -> `"8-10,14"` (contiguous runs collapsed)."""
    ids = sorted(set(ids))
    if not ids:
        raise ValueError("empty cpuset")
    runs: list[str] = []
    start = prev = ids[0]
    for cpu in ids[1:]:
        if cpu == prev + 1:
            prev = cpu
            continue
        runs.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = cpu
    runs.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(runs)


def pg_cpuset_overrides(
    pg_cores: int,
    *,
    taken_from: str | None,
    pg_cpus: str | None = None,
    donor_cpus: str | None = None,
) -> dict[str, str]:
    """Compose env moving `pg_cores - len(pg)` cores from the donor set to PG.

    Cores are taken from the LOW end of the donor so both cpusets stay
    contiguous (they are adjacent by construction: PG 8-10, vegeta 11-13). The
    donor must keep at least one core. Raises ValueError when the move is not
    possible; the caller records that as a lever that did NOT apply, never as a
    silently-skipped one.
    """
    donor_var = _DONOR_ENV_BY_NAME.get((taken_from or "").strip())
    if donor_var is None:
        raise ValueError(
            f"pg_cores lever declares taken_from={taken_from!r}; only "
            f"{sorted(_DONOR_ENV_BY_NAME)} are wired to a compose cpuset var"
        )
    pg = parse_cpuset(pg_cpus or COMPOSE_CPUSET_DEFAULTS["PG_CPUS"])
    donor = parse_cpuset(donor_cpus or COMPOSE_CPUSET_DEFAULTS[donor_var])
    need = pg_cores - len(pg)
    if need == 0:
        return {}
    if need < 0:
        raise ValueError(f"pg_cores lever would SHRINK PG ({len(pg)} -> {pg_cores})")
    if len(donor) - need < 1:
        raise ValueError(
            f"{taken_from} cannot spare {need} core(s): it has {len(donor)}"
        )
    moved = donor[:need]
    return {
        "PG_CPUS": format_cpuset(pg + moved),
        donor_var: format_cpuset(donor[need:]),
    }


@dataclass(frozen=True)
class LeverRealization:
    """What the fired levers actually change in THIS run.

    `env_overrides` are compose env vars the runner must apply (cpuset moves,
    CACHE_TTL_SECONDS). `not_applied` maps a fired lever to the reason it cannot
    be realized; a non-empty map means the PASS verdict rests on a lever that
    did not happen, which the caller must treat as a gate failure.
    """

    applied: list[str] = field(default_factory=list)
    not_applied: dict[str, str] = field(default_factory=dict)
    env_overrides: dict[str, str] = field(default_factory=dict)

    @property
    def fully_applied(self) -> bool:
        return not self.not_applied


def realize_levers(
    levers: list[LeverApplication],
    *,
    cache_ttl_seconds: int | None = None,
    pg_cpus: str | None = None,
    donor_cpus: str | None = None,
) -> LeverRealization:
    """Map fired levers onto concrete run-state changes (pure; unit-tested).

      write_share    -> rewritten into the EFFECTIVE workload mix (no env).
      cache_hit_rate -> spec/cache.md says the mechanism is a CACHE_TTL_SECONDS
                        raise, but spec/slo.yaml pre-registers NO TTL value. We
                        refuse to invent one: without an operator-supplied TTL
                        the lever is recorded NOT APPLIED.
      pg_cores       -> PG/donor cpuset move (compose env).
    """
    applied: list[str] = []
    not_applied: dict[str, str] = {}
    env: dict[str, str] = {}
    for lever in levers:
        if lever.name == "write_share":
            applied.append(lever.name)
        elif lever.name == "cache_hit_rate":
            if cache_ttl_seconds is None:
                not_applied[lever.name] = (
                    "spec/cache.md realizes this lever as a CACHE_TTL_SECONDS "
                    "raise but spec/slo.yaml pre-registers no TTL value; supply "
                    "one (LEDGERBENCH_CACHE_TTL_SECONDS) or the target hit rate "
                    "is only a claim"
                )
                continue
            env["CACHE_TTL_SECONDS"] = str(int(cache_ttl_seconds))
            applied.append(lever.name)
        elif lever.name == "pg_cores":
            try:
                env.update(
                    pg_cpuset_overrides(
                        int(lever.to_value), taken_from=lever.taken_from,
                        pg_cpus=pg_cpus, donor_cpus=donor_cpus,
                    )
                )
            except (TypeError, ValueError) as exc:
                not_applied[lever.name] = str(exc)
                continue
            applied.append(lever.name)
        else:  # pragma: no cover - LeverMachine only emits the three above
            not_applied[lever.name] = "unknown lever; no realization wired"
    return LeverRealization(applied=applied, not_applied=not_applied, env_overrides=env)


def params_for_applied_levers(
    spec_params: ResolvedWorkloadParams,
    lever_params: ResolvedWorkloadParams,
    applied: list[str],
) -> ResolvedWorkloadParams:
    """Spec params with ONLY the levers that were actually realized folded in.

    `LeverMachine` mutates its params for every lever it FIRES; a lever that
    fired but could not be applied (see `realize_levers`) must not show up in the
    effective workload, or the manifest would claim a change the run never made.
    """
    out = replace(spec_params)
    if "write_share" in applied:
        out.write_share = lever_params.write_share
    if "cache_hit_rate" in applied:
        out.cache_hit_rate = lever_params.cache_hit_rate
    if "pg_cores" in applied:
        out.pg_cores = lever_params.pg_cores
    return out


def apply_params_to_workload(wl: Workload, params: ResolvedWorkloadParams) -> Workload:
    """The EFFECTIVE workload the cells must run after the levers fired.

    Returns a NEW Workload (spec/ is frozen and must never be edited): the mix's
    create_invoice share is set to the lever's write_share with the difference
    absorbed by WRITE_SHARE_ABSORBER, and cache_construction.target_hit_rate is
    set to the lever's cache_hit_rate. `pg_cores` is topology, not workload, and
    is realized through the compose cpuset env (`pg_cpuset_overrides`).
    """
    eff = wl.model_copy(deep=True)
    create_pct_f = params.write_share * 100.0
    create_pct = int(round(create_pct_f))
    if abs(create_pct_f - create_pct) > 1e-6:
        raise ValueError(
            f"write_share {params.write_share} is not expressible as an integer "
            "mix percentage; the mix must stay integral (sums to 100)"
        )
    delta = eff.mix.create_invoice - create_pct
    absorber = getattr(eff.mix, WRITE_SHARE_ABSORBER)
    if absorber + delta < 0:
        raise ValueError(
            f"{WRITE_SHARE_ABSORBER} cannot absorb {delta} mix points "
            f"(it holds {absorber})"
        )
    eff.mix.create_invoice = create_pct
    setattr(eff.mix, WRITE_SHARE_ABSORBER, absorber + delta)
    # Re-run the sum==100 validator on the rewritten mix (model_copy skips it).
    eff.mix = type(eff.mix).model_validate(eff.mix.model_dump())
    eff.cache_construction.target_hit_rate = params.cache_hit_rate
    return eff


def effective_workload_record(
    spec_wl: Workload,
    effective_wl: Workload,
    params: ResolvedWorkloadParams,
    *,
    realization: LeverRealization,
    levers_fired: list[LeverApplication],
) -> EffectiveWorkload:
    """Spec-vs-effective echo for the ledger + manifest (F1 disclosure)."""
    return EffectiveWorkload(
        spec_mix=dict(spec_wl.mix.as_pairs()),
        effective_mix=dict(effective_wl.mix.as_pairs()),
        spec_write_share=spec_wl.mix.create_invoice / 100.0,
        effective_write_share=effective_wl.mix.create_invoice / 100.0,
        spec_cache_hit_rate=spec_wl.cache_construction.target_hit_rate,
        effective_cache_hit_rate=effective_wl.cache_construction.target_hit_rate,
        spec_pg_cores=_BENCH_PG_CORES,
        effective_pg_cores=params.pg_cores,
        write_share_absorbed_by=WRITE_SHARE_ABSORBER,
        levers_fired=[lv.name for lv in levers_fired],
        levers_applied=list(realization.applied),
        levers_not_applied=dict(realization.not_applied),
        env_overrides=dict(realization.env_overrides),
        note=(
            "spec/ is frozen; the levers produce this effective workload in run "
            "state only. Any difference between spec_* and effective_* was "
            "APPLIED to the measured cells."
        ),
    )


def create_only_targets(
    wl: Workload, *, out_dir: Path, count: int, seed: int, scale: str,
    base_url: str = "http://sut-go:8080",
) -> GeneratedTargets:
    """Targets file of ONLY POST /invoices (the create-only ladder).

    Reuses the validated body generator but with a create-only mix so the bodies
    and customer-id Zipf selection match the headline create path exactly.

    `base_url` MUST be absolute: vegeta's attack argv has no base-URL flag, so
    a schemeless `POST /invoices` target fails per-request at ~10 us without
    ever touching the SUT. That is exactly what the gate shipped from
    2026-06-06 until 2026-08-14: the "one-rung PG ceiling" it reported then
    (666.67 rps = 500 x 4/3) was a ladder of instant vegeta errors misread as
    saturation,
    and the reworked gate's no-usable-rung refusal caught it on the first full
    run attempt. The default is the go SUT's compose-network URL; the same
    host the mixed generator uses (live_runner._NET_URL_BY_LANG) and the SUT
    `measure_pg_create_knee` brings up.
    """
    create_only = wl.model_copy(deep=True)
    create_only.mix.get_invoice = 0
    create_only.mix.list_invoices = 0
    create_only.mix.pricing_quote = 0
    create_only.mix.create_invoice = 100
    create_only.mix.pdf_stub = 0
    create_only.mix.healthz = 0
    # Bypass the sum==100 validator path by constructing already-valid (100).
    return generate_targets(
        create_only, out_dir=out_dir, count=count, seed=seed, scale=scale,
        base_url=base_url,
    )
