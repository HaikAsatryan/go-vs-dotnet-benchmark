# FAIRNESS: every dial, both sides, and why

> Pre-registered: frozen before any measured run. The dials and rules below do
> not move after the first recorded run; the results write-up only adds numbers.

> **Specified vs captured.** The dials are as frozen. What the measured run
> (`results/20260814T051533-fedora/`) actually verified is a subset, and every
> gap is marked **[not captured]** in place. Read those markers as part of the
> claim, not as footnotes to it: a dial that was specified but not verified
> supports nothing.

## Principles

1. Idiomatic production stacks, not parity-forced lowest common denominators.
   Where philosophies differ (EF Core vs sqlc), the asymmetry is kept, named,
   and measured instead of hidden.
2. Every knob on either side is either (a) the documented default, or (b) set
   identically on both sides, or (c) an explicitly labeled variant axis, or (d)
   a deliberate exception listed in the asymmetry table with the direction it
   biases. There is exactly one (d): the EF compiled model, which favours .NET
   and therefore makes the headline conservative.
3. Anything that could bias the comparison gets its own validation gate before
   headline numbers count. Scope was frozen on 2026-08-13, the day before the run
   started: five pre-registered gates were retired outright at that point rather
   than left standing as promises nothing would keep, and they did not run. The
   measured run cleared every gate that survived. See the gate table below.

## Asymmetries kept on purpose

| axis | Go | .NET | treatment |
|---|---|---|---|
| Data layer | sqlc (hand SQL, codegen) | EF Core 10 (LINQ, change tracking) | headline; a raw-ADO.NET variant (hand SQL over Npgsql, the no-ORM floor) and a Dapper variant (micro-ORM middle point) bound the ORM cost stepwise, and both ran as full cells: EF Core costs about 20% more CPU per request than hand-written ADO.NET at the same rate, and Dapper sits within 2% of raw ADO. The DB-CPU-per-request column that was to separate app cost from query-plan cost was **descoped at scope freeze** and will not run, so query-shape parity rests on the SQL parity tests plus EF shape assertions, not on a measurement |
| Prepared statements | pgx auto-caches (default) | Npgsql Max Auto Prepare=0 (default) | disclosed; the "prepared parity" variant (=20) quantifies it and ran as a full cell: the .NET knee moves from 1,468 rps to 10,426 rps at the same 20 ms promise, with the app's own CPU per request barely changing. pgx never de-tuned; the Go-vs-.NET headline is taken from the parity cell so the comparison does not carry this default |
| Redis client model | connection pool (go-redis) | multiplexer (SE.Redis) | both idiomatic; Redis-side utilization reported. **[not captured]** Redis is not cgroup-sampled, so no utilization figure exists |
| GC posture (headline) | GOGC=100, GOMEMLIMIT unset | Server GC + DATAS on | both pure documented defaults under the same 12 GiB container limit; `manifest.knobs_by_cell` records the knobs per cell, so the `gc-default` cell's "no GC knobs set" is now evidenced rather than asserted |
| GC posture (memory-generous cell) | GOGC=off + GOMEMLIMIT=10GiB | DATAS off | explicitly labeled second cell of the GC axis. Posture only in the measured run: all 39 Go windows and 26 of 39 .NET windows failed a warmup gate, and no interleave block survives on both sides, so it supports one directional statement (Go held 6,619 MB anon against .NET's 212 MB) and nothing else |
| Compilation model | `go build`, static binary, no PGO profile supplied | `dotnet publish -c Release`: tiered JIT with dynamic PGO on, **no ReadyToRun and no Native AOT** | both are the documented default for their stack, and neither is tuned. Native AOT was not used: it does not cover the reflection and dynamic-codegen surface a typical .NET service (EF Core included) depends on, so publishing AOT would have measured a different application. The JIT's warm-up cost is exactly what the warm-up gates exist to exclude, and it is excluded, not amortised away |
| EF compiled model | no analogue exists or is needed: sqlc emits the SQL at build time | `dotnet ef dbcontext optimize` output committed under `Data/CompiledModels` and wired with `.UseModel(...)` | **a non-default EF optimisation, and the one asymmetry here that is not a default on both sides.** It removes EF's runtime model-build and lowers first-query cost. It therefore makes .NET look better than stock EF would, which biases *against* this paper's own conclusion; declaring it is cheaper than the alternative of leaving a reader to find it in `Program.cs` |
| Go data-layer composition | sqlc generates the list query (Q3) and the existence checks (Q7); the batched point read (`get_invoice_batch.go`) and the create transaction (`tx_create_invoice.go`) are hand-written pgx | EF Core LINQ throughout, including both of those paths | disclosed, because it means the two hottest paths in the mix (`GET /invoices/{id}` at 30%, `POST /invoices` at 15%) are EF Core against hand-written pgx, not against sqlc. The reason is a pgx limitation, not a tuning choice: pgx binds batch parameters at `Queue()` time and cannot propagate a `RETURNING` value between statements in one batch, so the create path cannot be generated, and the batched point read is not expressible in sqlc. Five of the seven generated methods are consequently unused and kept only so the generated file matches `spec/db-access.sql` |
| ADO / Dapper cells are not a pure no-ORM floor | Go's `/readyz` pings the pgx pool directly | .NET's `/readyz` takes `LedgerDbContext`, so the EF model is built and the context pool instantiated in EVERY cell, including `variant.ado` and `variant.dapper`. Both the compose healthcheck and the warm-up gate call it | disclosed, not fixed: changing it after the run would mean the committed code is no longer the code that produced the numbers. `/readyz` is not in the workload mix, so it costs no measured request, but it does mean the two no-ORM cells still pay EF's startup model build and keep a context pool resident. The effect is on .NET's side of the ledger, so it makes the ADO and Dapper knees slightly pessimistic, not flattering |
| Memory ceiling mechanism | container `mem_limit` only (no soft limit set in the headline) | container `mem_limit`; the CLR additionally applies its default hard heap limit (about 75% of the cgroup limit, a runtime default, not set by us) | identical container limits; the runtime-default difference is disclosed here rather than tuned away |
| Warmup | none needed (statically compiled binary) | tiered JIT + PGO | same multi-gate warmup both stacks must pass; under-warmed probes are excluded from fits by `analyze`; cold-start curve co-equal result. **[not captured]** the cold-start curve was descoped. Flagging is heavy and language-asymmetric: 368 of 618 windows (59.5%), Go 208/309 against .NET 160/309, because Go's per-second p99 wanders more at high rates. Several Go roll-ups therefore rest on one or two warm windows. Counts in `results/README.md` and in every published row |

## Identical by construction (enforced by spec/ + equivalence suite)

Wire contract (shapes, frozen error strings, 6-digit timestamps, money as
int64 minor units), read-path SQL shapes (canonical spec/db-access.sql),
transaction structure + batching intent (EF SaveChanges auto-batch vs pgx
SendBatch; round-trip ledger asserted equal), cache policy (serialize-then-cache,
TTL, DEL on create), logging contract (1 access line per request, bounded 8192,
block-on-full, lossless, code and test enforced; the isolated sink-cost
microbench was descoped at scope freeze, so the sink's cost is unmeasured in
isolation), pool sizes (single env var, manifest-asserted, verified warm at 24/24
in every cell), HTTP server limits, container resource model, core parallelism
(GOMAXPROCS and DOTNET_PROCESSOR_COUNT pinned to the SUT core count and asserted
from `/runtime-stats` per run).

**Correction: SQL parity is narrower than this section used to claim.** The
equivalence suite asserts the wire contract and the read-path statement shapes;
it does **not** assert byte-identical SQL against `spec/db-access.sql` for EF on
every statement. Three shape deviations are pinned and asserted in tests rather
than eliminated: EF sends a parameterized `LIMIT`/`OFFSET` where the hand-written
layers send a literal, EF adds a trailing `LIMIT 1` on the point read (both
plan-equivalent on the read path), and EF's atomic balance increment
(`ExecuteUpdate`, Q6 semantics, see spec/db-access.md) is its own round-trip
rather than folded into the item batch. sqlc additionally prepends a `-- name:`
comment to statements on the wire.

**[not captured]** the pool size (24/24, min=max) is pinned and asserted in the
manifest, but the connection sweep meant to *derive* it never ran, so 24 is a
chosen number applied equally to both sides rather than a per-stack optimum. The
logging sink microbench and no-drop test also did not run; the logging contract
is enforced by code and tests, but its cost is unmeasured in isolation.

## Known divergences (disclosed)

The first entry is outside the measured workload; the rest are inside it and are
part of what the headline ratio contains. Each is labelled with the side it
costs, because "idiomatic on both sides" only stays honest if the incidental
asymmetries are counted in both directions.

- **Explicit JSON `null` on a numeric field**: .NET treats it as a
  deserialization error (frozen body-400), Go as a zero value (field-level
  message). Never exercised by the workload or the equivalence vectors;
  documented at the validation site in both services.
- **Cache-miss read round-trips** (costs .NET): on a `GET /invoices/{id}` cache
  miss, Go issues the two frozen statements as one pipelined batch; EF awaits
  them as two sequential round-trips (the frozen two-statement SQL shape is held
  identical; EF cannot batch two arbitrary LINQ queries into one round-trip).
  The construction intends this to bite on about 20% of that endpoint's traffic
  (the 0.80 target hit rate), and the measured run is the first to check:
  realized hit rate **0.807** from Redis INFO keyspace deltas over all 618
  windows, so the miss path fires at close to its intended weight. The residual
  caveat is generalization, not weight: a fixed 1,000,000-request list replayed
  in order against a 300 s TTL is still more cache-resident than a production
  arrival process. The DB-CPU column that would quantify the round-trip cost
  itself was descoped and not captured.
- **pdf-stub existence probe** (RESOLVED; was: costs .NET, 8% of the
  mix): both sides now issue the Q1 header lookup only (`InvoiceExistsAsync`
  mirrors `db.InvoiceExists`).
- **Per-request filesystem and logger calls** (RESOLVED; was: costs
  .NET): the per-request `Directory.CreateDirectory` and per-request
  `ILoggerFactory.CreateLogger` were hoisted to startup, matching Go's
  single-logger / direct-write shape.
- **JSON serialization** (costs Go): .NET serializes through a
  System.Text.Json source-generated context (no reflection at runtime); Go uses
  `encoding/json`'s reflection-based `Marshal`. Both are the idiomatic default
  for their stack, and the cached read path is byte-passthrough on both sides,
  but on every non-cached response .NET holds the faster serializer.
- **Existence-probe shape** (symmetric): both sides reuse the
  full frozen header `SELECT` for the pdf-stub existence check and discard the
  columns rather than issuing a cheaper `EXISTS`, keeping the statement set
  identical to `spec/db-access.sql`. Parity was chosen over speed, now on both
  sides of the ledger.
- **Client-abort handling** (costs Go, bounded): when the load generator times
  out and drops a connection, ASP.NET's exception middleware records 499 and
  emits nothing else, while Go treats the surfaced cancellation as an internal
  error (two error log lines through the blocking queue plus a 500 body written
  to a dead socket) and the two access logs record 499 vs 500 for the same
  physical event. Both sides cancel the in-flight DB work. Bounded by
  construction: sub-knee abort rates are approximately 0 (7 non-2xx in
  358,673,386 requests across the whole run) and any probe with an error rate
  above 0.1% is excluded from fits by the pre-registered gate, which is what
  happens at the ladder walls. Details in
  services/dotnet/NOTES-equivalence.md.
- **Redis client resilience** (direction depends on failure mode; inert when
  Redis is healthy): go-redis v9 silently retries commands up to 3x on
  transient network errors, SE.Redis never retries commands; client timeouts
  are unpinned (3 s vs 5 s defaults). Under Redis distress Go absorbs faults
  the .NET side surfaces as 500s, feeding the error-rate gate asymmetrically.
  Kept: both are the idiomatic client defaults.

## Gates before any number counts

The pre-registered gate list, with the measured run's status against each.

| gate | measured run |
|---|---|
| equivalence suite green | **passed** (19 tests, at run start) |
| achieved==offered per probe | **passed** (ratio 1.0000 on every non-excluded window) |
| throttle counters zero, no OOM | **passed** on every aggregated window |
| error rate < 0.1% | **passed** sub-knee: 7 HTTP 500s in 358,673,386 requests. The run-wide 0.25% figure is transport-level errors at the ladder walls, where the rule excludes those probes from the fits |
| warmup gates passed | ran per probe and the exclusion is applied by `analyze`; 2 of 6 declared gates (`hot_set_touched`, `major_faults_asserted_equal`) are **not implemented**; flagging is heavy (59.5%) and language-asymmetric, and one cell (`gc-generous`) is 100% flagged on the Go side (39/39, against 26/39 on the .NET side), which leaves no warm paired block and makes the cell posture only |
| PG headroom evidence (capacity gate) | **passed, with a caveat that belongs on the record**: PostgreSQL tracked all 12 rungs to 3,715 create-only rps on 3 cores without breaking (a floor, not a ceiling). The 1.72x margin was taken against 2,160 rps of *projected* demand, which is a pre-run planning estimate (500 rps x 8 assumed knee x 0.54 DB-touch share), not the rates the cells later ran at (9,244 to 13,312 rps, implying ~5,000 to 7,200 rps of mixed DB load). PostgreSQL was never observed to break in the cells either, but the gate did not test it at their realized rates. No levers fired; the effective workload equals the frozen spec workload |
| CO validation suite (stall, sustained degradation, self-saturation) | **descoped at scope freeze**, will not run; CO safety rests on per-probe achieved==offered + smoke's docker-pause stall check |
| oha tail cross-check | **descoped at scope freeze**, will not run |
| logging sink microbench + no-drop test | **descoped at scope freeze**; the logging contract stays enforced by code and tests; its isolated cost stays unmeasured |
| connection sweep | **descoped at scope freeze**; the pool stays a disclosed pinned choice (24/24, min=max, enforced identical both sides), not a derived optimum |
| DB-CPU-per-request column (PG/vegeta/dockerd cgroup deltas) | **descoped at scope freeze**, will not run; only the two SUT containers are cgroup-sampled, so the app-cost vs query-plan-cost split stays unmeasured and its absence is disclosed wherever it limits a claim |

The five descoped rows were retired deliberately at the scope freeze: they had never
run, and pre-registering checks that will never run is worse than disclosing
their absence. Every remaining row stays load-bearing.

## Single-host accounting

SUT alone on one CCD (cpuset, SMT siblings idle); PG, Redis, vegeta, runner on
the other; host daemons pinned to a housekeeping core (dockerd is on the hot
logging path); observability stack down during recorded runs; page cache warmed
identically (fault counts asserted approximately equal).

**[not captured]** the tuning is applied by `infra/host-setup.sh --tune` (and
re-applied on every resume, since a reboot resets governor, boost and CPU
pinning), but the manifest's `host_state`, `topology` and `postgres_config`
blocks record `source: not_measured` with a note explaining that nothing reads
them back off the box, so the run cannot *evidence* the governor, boost, C-state,
THP, IRQ or cpuset placement it ran under. dockerd's CPU is not sampled either.
The fault-count assertion is one of the two unimplemented warmup gates. Both
stacks demonstrably ran under the same host at the same time, interleaved, but
"same" is inferred from the procedure rather than read back from the artifacts.
