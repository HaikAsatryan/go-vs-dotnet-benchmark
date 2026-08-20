"""Full-disclosure RunManifest schema + writer.

One `results/<run_id>/manifest.json` per run: the complete record needed to make
the headline numbers auditable (all knob values echoed). The runner phases
populate the sections as they execute. Optional fields default to None/empty so
a partially-completed run still serializes.

A field that was NEVER MEASURED must say so. Empty lists and bare nulls read as
"zero" to a reader (the 2026-06-06 manifest published `fault_counts: []`,
`cache_stats: {null,null,null}` and `errors: {count: 0}` for a run that measured
none of them), so the aggregate sections below carry an explicit `source` /
`note` and count the windows they were built from. The guarantee is per SECTION,
not only per row: a list-valued aggregate that stayed empty is named in
`aggregate_provenance` with the reason, and the sections no code populates
(host_state / topology / postgres_config) carry their own `source` marker so an
empty object cannot read as "measured and empty".

schema_version 2 (2026-08): knobs are recorded PER CELL and never cross-copied
between languages; capacity_gate, effective_workload and working_set sections
added; the aggregate sections carry provenance.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .util import paths
from .util.clock import rfc3339_micros
from .util.proc import run

MANIFEST_SCHEMA_VERSION = 2

NOT_MEASURED = "not_measured"


class Identity(BaseModel):
    """WHICH code produced the numbers.

    `git_sha` / `git_dirty` are the values read when the run STARTED and are
    never overwritten afterwards: a resume runs from whatever is checked out
    then, and silently restamping the sha would erase the fact that the run
    began on different code. A resume from a different commit appends to
    `git_sha_changed_during_run` instead (empty on a single-commit run).
    """

    model_config = ConfigDict(extra="forbid")
    run_id: str
    git_sha: str | None = None
    git_dirty: bool | None = None
    # {"at": rfc3339, "git_sha": ..., "git_dirty": ...} per observed change.
    git_sha_changed_during_run: list[dict] = Field(default_factory=list)
    schema_sql_sha256: str | None = None      # spec/schema.sql hash
    generated_code_hashes: dict[str, str] = Field(default_factory=dict)
    config_hash: str | None = None            # sha256 over the whole spec/ tree


class Versions(BaseModel):
    """Runtime/library versions in force during the run.

    NOTHING populates this yet: the `/runtime-stats` build block carries
    `runtime_version` and `npgsql_version` per service, but no code reads them
    back into the manifest, and the image digests are pinned in the Dockerfiles
    rather than resolved at run time. `source` therefore stays `not_measured`:
    an all-null Versions is the ABSENCE of a reading, never "measured and
    unknown". The authoritative table lives in the README, sourced from
    `go.mod`, `Ledgerline.csproj` and the digest-pinned compose images.
    """

    model_config = ConfigDict(extra="allow")
    source: str = NOT_MEASURED
    note: str = (
        "not populated by the runner: versions are pinned in go.mod, "
        "Ledgerline.csproj and the digest-pinned Dockerfiles/compose images, "
        "and nothing reads them back off the running containers"
    )
    # Image digests (tags now; digest pinned at smoke).
    image_digests: dict[str, str] = Field(default_factory=dict)
    # Runtime/library versions read from /runtime-stats build block.
    go: str | None = None
    dotnet: str | None = None
    pgx: str | None = None
    npgsql: str | None = None
    sqlc: str | None = None
    redis: str | None = None
    postgres: str | None = None
    vegeta: str | None = None


class Knobs(BaseModel):
    """Every variant lever. null where not applicable to a language."""

    model_config = ConfigDict(extra="forbid")
    db_pool_max: int | None = None
    db_pool_min: int | None = None
    db_conn_lifetime_seconds: int | None = None
    redis_pool_max: int | None = None
    cache_ttl_seconds: int | None = None
    log_queue_len: int | None = None
    gogc: str | None = None
    gomemlimit: str | None = None
    gc_dynamic_adaptation_mode: str | None = None
    max_auto_prepare: int | None = None
    data_layer: str | None = None             # sqlc | efcore | ado | dapper (live-read, asserted)
    gomaxprocs: int | None = None             # pinned == SUT_CORES (go)
    processor_count: int | None = None        # asserted == SUT_CORES on Linux


class CellKnobs(BaseModel):
    """The knob values a SINGLE cell ran with, per language.

    Knobs are per cell, not per run: `headline.mixed.gc-default` runs at compose
    defaults and `headline.mixed.gc-generous` at GOGC=off / DATAS=0, so one
    run-level pair of Knobs rows can only ever describe the last cell executed
    (the 2026-06-06 manifest did exactly that). Language-specific knobs stay on
    their own side: GOGC/GOMEMLIMIT/GOMAXPROCS are Go-only and
    DOTNET_GCDynamicAdaptationMode/MAX_AUTO_PREPARE/DATA_LAYER are .NET-only, so
    a reader can never mistake a cross-copied value for a measured one.
    """

    model_config = ConfigDict(extra="forbid")
    cell_id: str
    extra_env: dict[str, str] = Field(default_factory=dict)   # verbatim cell env
    go: Knobs = Field(default_factory=Knobs)
    dotnet: Knobs = Field(default_factory=Knobs)


class HostState(BaseModel):
    """Host tuning actually in force during the run.

    NOTHING populates this yet: `infra/host-setup.sh` applies the tuning and the
    doctor phase checks a subset, but no code reads the applied state back into
    the manifest. `source` therefore stays `not_measured`; an all-null HostState
    is the ABSENCE of a reading, never "measured and default".
    """

    model_config = ConfigDict(extra="allow")
    source: str = NOT_MEASURED
    note: str = (
        "not populated by the runner: host tuning is applied by "
        "infra/host-setup.sh and only partially re-checked by the doctor phase; "
        "nothing reads it back into the manifest"
    )
    governor: str | None = None
    boost_off: bool | None = None
    cstate_cap: str | None = None
    thp_mode: str | None = None
    irq_mask: str | None = None
    cpuaffinity_dropin_present: bool | None = None
    isolcpus: str | None = None
    nohz_full: str | None = None
    turbostat_snapshot: dict | None = None
    btrfs_cow_off: bool | None = None
    noatime: bool | None = None
    observability_stack: str | None = None    # must be "down" for headline
    tier: str | None = None                   # "1" | "2"


class ContainerTopology(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cpuset: str | None = None
    mem_limit: str | None = None
    ccd: str | None = None


class Topology(BaseModel):
    """Per-container cpuset / memory limit / CCD placement.

    NOT populated by the runner: the cpusets live in infra/compose.bench.yaml and
    are never read back off the running containers (the capacity gate's cpuset
    LEVER, when it fires, is disclosed in `capacity_gate.levers_applied` +
    `effective_workload.env_overrides` instead). An empty `containers` map means
    nothing was recorded, not "no pinning".
    """

    model_config = ConfigDict(extra="forbid")
    source: str = NOT_MEASURED
    note: str = (
        "not populated by the runner: cpusets/mem limits are declared in "
        "infra/compose.bench.yaml and never read back off the containers"
    )
    containers: dict[str, ContainerTopology] = Field(default_factory=dict)
    ccd_mapping: dict[str, str] = Field(default_factory=dict)


class PostgresConfigEcho(BaseModel):
    """The R1 three-way split, each value tagged inherited/rescaled/pg18-default.

    NOT populated by the runner: no phase issues the `SHOW`/pg_settings read that
    would fill it, so `values` stays empty with `source: not_measured` rather
    than presenting an empty echo as "PG ran with nothing set".
    """

    model_config = ConfigDict(extra="allow")
    source: str = NOT_MEASURED
    note: str = (
        "not populated by the runner: no phase reads pg_settings back; the "
        "declared values live in spec/postgres/postgresql.conf"
    )
    values: dict[str, dict] = Field(default_factory=dict)


class AchievedVsOffered(BaseModel):
    model_config = ConfigDict(extra="forbid")
    probe_id: str
    offered_rate: float
    achieved_rate: float
    ratio: float
    max_workers: int | None = None
    connections: int | None = None
    timeout_s: float | None = None


class ThrottleCounters(BaseModel):
    """cpu.stat throttle counters for one container, summed across probe windows.

    NOT a per-measured-window delta. cpu.stat's nr_throttled/throttled_usec are
    CUMULATIVE over the container's lifetime and are read once, at the END of
    each probe window (cgroups.py), so each term covers that container's whole
    life up to that point: warmup + head-of-window discard + measured window.
    The SUT is force-recreated per probe, so the terms do not double-count across
    probes, but a non-zero value must be read as "throttling happened somewhere
    in that container's life", not "in the measured window".
    """

    model_config = ConfigDict(extra="forbid")
    container: str
    nr_throttled: int = 0
    throttled_usec: int = 0
    windows: int = 0                          # probe windows summed here
    source: str = NOT_MEASURED


class FaultCounts(BaseModel):
    """memory.stat pgmajfault/pgfault deltas over the measured windows.

    None (not 0) when the run's probe artifacts carry no fault reads: a zero
    major-fault count is a strong claim (no page-cache misses at all) and must
    never be manufactured by a default.
    """

    model_config = ConfigDict(extra="forbid")
    container: str
    major_faults: int | None = None
    minor_faults: int | None = None
    windows: int = 0
    source: str = NOT_MEASURED


class ResourceDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    container: str
    cpu_usage_usec_delta: int = 0             # summed over the measured windows
    mem_anon_p99_bytes: int | None = None     # max of the per-window anon p99
    mem_peak_bytes: int | None = None         # max of the per-window memory.peak
    windows: int = 0
    source: str = NOT_MEASURED


class Errors(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = 0                            # non-2xx responses over all probes
    rate: float = 0.0
    total_requests: int = 0
    status_codes: dict[str, int] = Field(default_factory=dict)
    probe_summaries: int = 0                  # vegeta summaries aggregated
    source: str = NOT_MEASURED


class CacheStats(BaseModel):
    """Redis INFO keyspace_hits/misses delta, summed over measured windows.

    `source` is explicit: `not_measured` means no probe recorded a Redis INFO
    sample, which is NOT the same as "0 hits". spec/cache.md constructs a 0.80
    target hit rate; only a measured hit_rate may be compared against it.
    """

    model_config = ConfigDict(extra="forbid")
    keyspace_hits: int | None = None
    keyspace_misses: int | None = None
    hit_rate: float | None = None             # asserted vs target (cache.md)
    windows: int = 0
    source: str = NOT_MEASURED
    note: str = ""


class AggregateProvenance(BaseModel):
    """Per-SECTION provenance for the list-valued aggregates.

    A row-level `source` cannot rescue an EMPTY list: `throttle_counters: []`
    reads as "measured, nothing to report" (the 2026-06-06 manifest published
    exactly that). Each list section names here what it was built from, and stays
    `not_measured` when finalization found no artifact to build it from.
    """

    model_config = ConfigDict(extra="forbid")
    achieved_vs_offered: str = NOT_MEASURED
    throttle_counters: str = NOT_MEASURED
    fault_counts: str = NOT_MEASURED
    resource_deltas: str = NOT_MEASURED


class CapacityGateStatus(BaseModel):
    """The gate verdict, stamped so every downstream artifact inherits it.

    `degraded` is True whenever the gate did NOT return `pass`, including a run
    that aborted before a single cell (it fails safe: an aborted run's partial
    artifacts must not read as clean). `degraded_allowed_by` is what separates
    the two cases: it names the opt-out flag that let the measured cells run
    ANYWAY, and is null on a run that stopped. The headline of a degraded run is
    not a clean cost-at-a-latency-promise number, so the caveat travels with the
    manifest rather than living only in a verdict.json nobody reads.

    `pg_knee_rps` is the MEASURED create-only knee at `pg_knee_measured_at_cores`
   : never a scaled number. The linear core-scaling projection the lever logic
    used to decide whether to spend the pg_cores lever is
    `pg_capacity_projected_rps`, and `pg_capacity_projection_basis` spells out
    the arithmetic. The 2026-06-06 run published `pg_knee_rps: 666.67`, which was
    a measured 500.0057 rps scaled by 4/3; the two must never share a field.
    """

    model_config = ConfigDict(extra="forbid")
    verdict: str | None = None
    detail: str = ""
    pg_knee_rps: float | None = None          # MEASURED, at pg_knee_measured_at_cores
    pg_knee_measured: bool | None = None
    pg_knee_usable_rungs: int | None = None
    pg_knee_measured_at_cores: int | None = None
    # Linear core-scaling PROJECTION (assumption, not a measurement).
    pg_capacity_projected_rps: float | None = None
    pg_capacity_projection_basis: str = ""
    projected_db_rps: float | None = None
    headroom_factor: float | None = None
    levers_fired: list[str] = Field(default_factory=list)
    levers_applied: list[str] = Field(default_factory=list)
    levers_not_applied: dict[str, str] = Field(default_factory=dict)
    remeasured_pg_knee_rps: float | None = None
    degraded: bool = False
    degraded_allowed_by: str | None = None    # the flag that permitted the run


class EffectiveWorkload(BaseModel):
    """Spec-vs-effective workload: what the cells ACTUALLY ran.

    spec/ is frozen (config_hash), so the capacity-gate levers can only alter run
    state. This section is the disclosure of that alteration: read it next to
    identity.config_hash to see spec-vs-effective at a glance.
    """

    model_config = ConfigDict(extra="forbid")
    spec_mix: dict[str, int] = Field(default_factory=dict)
    effective_mix: dict[str, int] = Field(default_factory=dict)
    spec_write_share: float | None = None
    effective_write_share: float | None = None
    spec_cache_hit_rate: float | None = None
    effective_cache_hit_rate: float | None = None
    spec_pg_cores: int | None = None
    effective_pg_cores: int | None = None
    write_share_absorbed_by: str | None = None
    levers_fired: list[str] = Field(default_factory=list)
    levers_applied: list[str] = Field(default_factory=list)
    levers_not_applied: dict[str, str] = Field(default_factory=dict)
    env_overrides: dict[str, str] = Field(default_factory=dict)
    note: str = ""

    @property
    def differs_from_spec(self) -> bool:
        return (
            self.spec_mix != self.effective_mix
            or self.spec_cache_hit_rate != self.effective_cache_hit_rate
            or self.spec_pg_cores != self.effective_pg_cores
        )


class WorkingSet(BaseModel):
    """The DISTINCT keys the READ path replays.

    "5 000 000 invoices seeded" is not "5 000 000 invoices measured": the targets
    file is a finite, replayed list, so the read path's real working set is the
    number of DISTINCT ids in it, replayed every target_count/rate seconds. With
    a 300 s cache TTL a replay period of a couple of seconds means the read path
    is essentially cache-resident: which the reader can only see if this
    section exists.

    Scope, precisely: these counts describe the ids the targets file NAMES. The
    write path is not bounded by them: create_invoice INSERTs rows whose ids
    appear in no target and are never read back, so this is the read-side
    working set, not "every row the run touched".

    `target_count` counts REQUESTS, not lines: a POST target spans a request
    line, a header line and an `@body` reference, so the file itself is several
    times longer.

    `distinct_customer_ids` is the UNION of two sources (list-invoices URLs and
    create-invoice request bodies) and is therefore larger than either;
    `distinct_customer_ids_list_urls` / `_create_bodies` carry the two components
    separately so a number quoted from one of them can be matched exactly.
    """

    model_config = ConfigDict(extra="forbid")
    targets_file: str | None = None
    target_count: int | None = None           # REQUESTS in the generated file
    distinct_invoice_ids: int | None = None   # GET /invoices/{id} + pdf-stub URLs
    distinct_customer_ids: int | None = None  # union of the two fields below
    distinct_customer_ids_list_urls: int | None = None     # /customers/{id}/invoices
    distinct_customer_ids_create_bodies: int | None = None  # POST body customer_id
    seeded_invoices: int | None = None
    seeded_customers: int | None = None
    cache_ttl_seconds: int | None = None
    # WHERE cache_ttl_seconds came from (lever override / env / compose default);
    # `not_measured` means the effective TTL could not be established.
    cache_ttl_source: str = NOT_MEASURED
    # offered rate (as a string key) -> seconds to replay the whole targets file
    replay_period_seconds: dict[str, float] = Field(default_factory=dict)
    note: str = ""


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = MANIFEST_SCHEMA_VERSION
    created_utc: str = Field(default_factory=rfc3339_micros)
    identity: Identity
    versions: Versions = Field(default_factory=Versions)
    # Run-level, CELL-INDEPENDENT knobs only (core pinning). Per-cell knob values
    # live in knobs_by_cell; nothing cell-scoped may be written here.
    knobs_go: Knobs = Field(default_factory=Knobs)
    knobs_dotnet: Knobs = Field(default_factory=Knobs)
    knobs_by_cell: dict[str, CellKnobs] = Field(default_factory=dict)
    capacity_gate: CapacityGateStatus = Field(default_factory=CapacityGateStatus)
    effective_workload: EffectiveWorkload = Field(default_factory=EffectiveWorkload)
    working_set: WorkingSet = Field(default_factory=WorkingSet)
    # Gates spec/slo.yaml declares that the runner does NOT implement. Recorded
    # so an unimplemented gate can never be read as a passed one.
    unimplemented_warmup_gates: list[str] = Field(default_factory=list)
    host_state: HostState = Field(default_factory=HostState)
    topology: Topology = Field(default_factory=Topology)
    postgres_config: PostgresConfigEcho = Field(default_factory=PostgresConfigEcho)
    achieved_vs_offered: list[AchievedVsOffered] = Field(default_factory=list)
    throttle_counters: list[ThrottleCounters] = Field(default_factory=list)
    fault_counts: list[FaultCounts] = Field(default_factory=list)
    resource_deltas: list[ResourceDelta] = Field(default_factory=list)
    # What each of the four list sections above was built from; a section that
    # stayed [] says so here instead of reading as a measured emptiness.
    aggregate_provenance: AggregateProvenance = Field(default_factory=AggregateProvenance)
    cache_stats: CacheStats = Field(default_factory=CacheStats)
    errors: Errors = Field(default_factory=Errors)
    # Resource accounting is OFF on non-Linux (smoke); recorded explicitly.
    resource_accounting: str = "enabled"
    # Set when finalize_manifest could not aggregate an artifact. Finalization
    # never raises (it must not lose a completed run), so the failure is recorded
    # here rather than swallowed.
    finalization_error: str | None = None

    # ----- persistence ----------------------------------------------------- #
    def path(self) -> Path:
        return paths.run_dir(self.identity.run_id) / "manifest.json"

    def save(self) -> None:
        paths.write_text_atomic(self.path(), self.model_dump_json(indent=2))

    @classmethod
    def load(cls, run_id: str) -> "RunManifest | None":
        p = paths.run_dir(run_id) / "manifest.json"
        if not p.exists():
            return None
        return cls.model_validate_json(p.read_text(encoding="utf-8"))


def new_manifest(run_id: str, *, config_hash: str | None = None) -> RunManifest:
    return RunManifest(identity=Identity(run_id=run_id, config_hash=config_hash))


# --------------------------------------------------------------------------- #
# provenance helpers
# --------------------------------------------------------------------------- #
_INVOICE_ID_RE = re.compile(r"/invoices/(\d+)")
_CUSTOMER_ID_RE = re.compile(r"/customers/(\d+)/invoices")
_REQUEST_LINE_RE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD) \S")


def working_set_from_targets(targets_path: Path) -> WorkingSet:
    """Count the DISTINCT ids a generated vegeta targets file NAMES.

    Invoice ids come from the `/invoices/{id}` URLs (get_invoice + pdf_stub);
    customer ids from the `/customers/{id}/invoices` URLs UNION the customer_id
    in each referenced create-invoice body (the create path carries its id in the
    body, not the URL). The two customer sources are also reported separately, so
    "the customers the read path lists" and "the customers the write path
    invoices" can each be quoted exactly.

    `target_count` counts REQUESTS, not lines (a POST target spans a request
    line, a Content-Type header and an `@body` reference).

    This bounds the READ path only: create_invoice INSERTs invoice ids that
    appear in no target. Pure file read; no docker, no network.
    """
    invoice_ids: set[str] = set()
    url_customer_ids: set[str] = set()
    body_customer_ids: set[str] = set()
    body_refs: list[str] = []
    requests = 0
    for raw in targets_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("@"):
            body_refs.append(line[1:])
            continue
        if not _REQUEST_LINE_RE.match(line):
            continue                    # header line (Content-Type: ...)
        requests += 1
        m = _INVOICE_ID_RE.search(line)
        if m:
            invoice_ids.add(m.group(1))
        m = _CUSTOMER_ID_RE.search(line)
        if m:
            url_customer_ids.add(m.group(1))

    bodies_read = 0
    for ref in body_refs:
        if "create_" not in ref:
            continue                    # quote bodies carry no id
        body_path = (targets_path.parent / ref)
        try:
            doc = json.loads(body_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cid = doc.get("customer_id")
        if cid is not None:
            body_customer_ids.add(str(cid))
            bodies_read += 1

    return WorkingSet(
        targets_file=targets_path.name,
        target_count=requests,
        distinct_invoice_ids=len(invoice_ids),
        distinct_customer_ids=len(url_customer_ids | body_customer_ids),
        distinct_customer_ids_list_urls=len(url_customer_ids),
        distinct_customer_ids_create_bodies=len(body_customer_ids),
        note=(
            f"distinct ids counted over {requests} generated REQUESTS (the file "
            f"itself is longer: a POST target spans several lines); "
            f"{bodies_read} create bodies read for their customer_id. The "
            "targets file is replayed in order, so these are the only keys the "
            "READ path asks for; the write path additionally inserts invoice "
            "ids that appear in no target. distinct_customer_ids is the UNION "
            f"of {len(url_customer_ids)} list-invoices URL customers and "
            f"{len(body_customer_ids)} create-body customers"
        ),
    )


def git_identity() -> tuple[str | None, bool | None]:
    """(git_sha, dirty) for the repo the runner is executing from.

    Read-only git plumbing; returns (None, None) when git is absent or the tree
    is not a repository (the manifest then records the absence, not a guess).

    `results/` is excluded from the dirty check: the run WRITES its own
    artifacts there (ledger.json exists before the identity stamp), so without
    the exclusion every run, including one launched from a pristine commit,
    would stamp itself dirty. Run outputs cannot alter the measured binaries;
    an untracked SOURCE file still counts as dirty (docker builds copy the
    tree, so it would ship).
    """
    root = str(paths.repo_root())
    sha = run(["git", "-C", root, "rev-parse", "HEAD"], timeout=15)
    if not sha.ok:
        return None, None
    status = run(
        ["git", "-C", root, "status", "--porcelain", "--", ":(exclude)results"],
        timeout=30,
    )
    dirty = bool(status.stdout.strip()) if status.ok else None
    return sha.stdout.strip(), dirty
