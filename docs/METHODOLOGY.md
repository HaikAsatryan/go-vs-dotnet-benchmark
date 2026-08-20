# METHODOLOGY: how the numbers are produced

> Pre-registered: this procedure and `spec/` are frozen before any measured run
> (every run manifest records a `config_hash` over all of `spec/`). Results are
> appended; the method does not move after the data exists.

> **Specified vs captured.** This document describes the method. It is *not* a
> description of what any particular run captured. The measured run
> (`results/20260814T051533-fedora/`) executed a subset of it; every gap is
> marked inline below with **[not captured]** and summarised in
> [Coverage of the measured run](#coverage-of-the-measured-run). Nothing here is
> softened after the fact: the procedure stands as pre-registered, and the run is
> judged against it.

## Headline metric

Cost at a fixed latency promise: at the knee rate where the mixed workload's
p99 crosses 20 ms (spec/slo.yaml), report per-request CPU-ms (cgroup
`cpu.stat` delta over all serviced requests) and steady-state anonymous memory
(p99 of `memory.stat.anon` over the window), as a Go:.NET ratio and an
illustrative cost per million requests (**[not captured]** the dollar figure is
omitted; it would only rescale the CPU ratio by a chosen vCPU price, so the ratio
is published instead). The p99 is the **pooled** p99 of all request latencies in
a probe's measured window (the load generator's report over the full window),
never a percentile of per-second percentiles. The per-second p99 series exists
too, but is used only for stationarity and warmup gates. The design needs
SUT-bound knees, not big numbers.

Two corrections the measurements forced on this section, kept here because they
are part of the method now:

- **Rate-independence is a hypothesis, not a premise.** The design assumed
  per-request cost is flat below saturation. It is, for Go (CoV 7.4% over the 19
  `prepared-parity` rungs); it is not, for .NET (CoV 19.3%, falling from 0.648
  CPU-ms/req at 500 rps to 0.308 at 13,312 rps). A single-operating-point cost
  ratio must therefore be published **with its rate**, and the cost-vs-rate curve
  published alongside it.
- **Equal rate is not equal latency promise.** If one stack meets the SLO at the
  chosen rate and the other does not, the resulting ratio is cost at equal
  offered load, and must be labelled that way. Cost at a fixed latency promise
  requires each stack measured at *its own* SLO-crossing rate. **[not
  captured]**: the runner fits one **pooled** knee per cell (go and .NET probe
  p99s pooled into the same rungs), chooses the confirm rates from it, and so
  measures both stacks at one shared rate. Per-language refits over the same
  ladder rungs are published in the run report (.NET crosses at 10,426 rps in
  `prepared-parity`; Go does not cross inside any measured ladder), but a
  *measurement* at each stack's own knee needs a run whose confirm stage is
  placed there, and locating Go's knee needs a ladder that goes higher than this
  one did.

## Load + knee procedure

Open-loop constant-rate (vegeta). Geometric ladder (step 1.2x, at least 3
interleaved reps per rung), isotonic fit of p99-vs-rate, knee = SLO crossing, CI
bootstrapped over reps, at least 10 confirmation reps in randomized complete
blocks (block = pairing key), 10-minute soak. Stationarity gate (Mann-Kendall on
per-second p99) per probe; supra-knee probes report saturation behavior only. The
ladder runs ascending only (a descending hysteresis pass was descoped
2026-08-13).

**Descoped at scope freeze:** the once-planned CO validation suite and oha tail
cross-check were removed and will not run. The CO evidence is per-probe
achieved==offered (ratio 1.0000 on every non-excluded window, manifest
`achieved_vs_offered`). The ladder ran ascending only, so no hysteresis band
exists. The bootstrap interval is emitted but is **not** an inferential CI here:
the SLO-crossing bracket carries as few as one measurement-grade rep against the
pre-registered minimum of 10, so it is a spread indicator over a handful of reps.
The live fit that drives the run also pools both languages' probes into each
rung, making the knee a property of the pooled sample rather than of either
stack; the report carries per-language refits beside it, which is the correct
shape, but the confirm rates the run executed were still chosen from the pooled
fit.

## Warmup

Every probe is warmed before its measured window, against the same symmetric
gate set for both stacks: per-endpoint min call counts, p99+p999 flat, GC
steady (DATAS heap count / Go cycle cadence), pools full, hot set touched
(fault counts asserted approximately equal). Warmup traffic runs at a fixed
sub-knee rate; the per-probe stationarity gate plus a 30 s discard at the head of
the measured window guard against residual JIT re-tiering at the measured rate.
Probes that fail the warmup gates are excluded from fits and confirms (a rung
losing all its reps fails the run rather than fitting around the hole).
Durations are published per language; the cold-start curve is a co-equal
result.

**[not captured]** two of the six declared gates, `hot_set_touched` and
`major_faults_asserted_equal` (`spec/slo.yaml`), are not implemented, and their
absence is not surfaced in the per-probe warmup records; a probe can be recorded
warm without them ever having been evaluated. The exclusion rule above *is*
applied by `analyze`, which prints used/flagged-excluded counts per row and marks
posture-only rows `*`. Two consequences must be read with the numbers.

First, flagging is heavy and **language-asymmetric**: 368 of 618 windows flagged
(59.5%), Go 208/309 (67.3%) against .NET 160/309 (51.8%), because Go's
per-second p99 wanders more at high rates and trips `p99_flat` / `p999_flat`.
Several Go roll-ups therefore rest on one or two warm windows, and the counts
ride along with every published row.

Second, in `gc-generous` *all* 39 Go probes are flagged, since `GOGC=off` cannot
satisfy `gc_steady` by construction; its .NET side is flagged 26 of 39 with no
declared cause, so the whole cell is posture evidence rather than a gated
measurement. A future run must either drop the `gc_steady` gate for a
GC-disabled posture (and say so) or drop the cell.

## Statistics

Within-run percentile uncertainty separated from between-run spread (dot
plots, median, min-max). Decision rule: paired-difference CI (paired by
block) against a pre-registered 5% equivalence margin. p999 in the appendix
from pooled raw samples (at least 10^6) with order-statistic CIs.

**[not captured]** the paired-difference CI runs and is published, one verdict
per offered rate, but only one cell produced paired blocks at all: `gc-default`
p99 at 1,244 rps (Go 12.6 ms vs .NET 30.37 ms, -58.49%, paired-t
[-758.01%, +641.02%] over 2 blocks, d_z -0.75) and at 1,493 rps (Go 5.463 vs
.NET 30.56, -82.12%, [-366.76%, +202.52%] over 3 blocks, d_z -0.72). Both
intervals span zero, so the published verdict is direction and effect size only.
The other four cells yield no paired verdict: a block pairs only when both
languages are measurement-grade in it, and in those cells one language has no
warm confirm window at the rate. The p999 appendix with order-statistic CIs was
not produced; neither were the dot plots of between-run spread, there being one
run.

## Resource accounting

cgroup v2 ground truth (paths resolved via /proc/<pid>/cgroup): SUT CPU delta.
50 ms anon sampling; `memory.peak` and runtime heap metrics as caveated
secondaries; throttle counters must be zero (cpuset headline); OOM = reported
failure. (The once-planned PG / vegeta / dockerd cgroup delta columns, the
DB-CPU-per-request attribution among them, were descoped at scope freeze: the
SUT-vs-SUT comparison is internally valid without them, and their absence is
disclosed wherever it limits a claim.) Core parallelism is pinned, not inferred:
GOMAXPROCS and DOTNET_PROCESSOR_COUNT are set to the SUT core count and asserted
from `/runtime-stats` per run.

**[not captured]** only the two SUT containers are cgroup-sampled, so the split
between "app cost" and "query-plan cost" is *not* measured (and per the descope
above, will not be). Throttle counters and OOM counts were checked and are zero
on every aggregated window; major faults were 0 (.NET) and 12 (Go) over the whole
run. `/runtime-stats` GC counters are polled live for the warmup gates but not
flushed to disk (descoped), so GC collection counts and pause totals are
unavailable. Cache hit-rate telemetry *is* captured: `manifest.cache_stats`
records a realized 0.807 from Redis INFO keyspace deltas over all 618 windows.

## Run matrix

Mixed headline across the GC-posture axis (defaults vs memory-generous, both
runtimes); DB-write capacity gate first; variants (via
`LEDGERBENCH_RUN_VARIANTS`): raw-ADO.NET, Dapper, prepared parity, the final
matrix after the scope freeze removed the once-planned isolation runs,
warmup/cold curves, quota-vs-cpuset, boost-on and memory-pressure sweep.
Resumable at probe granularity; manifests + small text artifacts committed.
The raw per-window traces and `.bin` captures are regenerable and are not
published (see `results/README.md`).

The measured run executed all five cells: both mixed GC-posture cells and all
three data-layer variants. The ORM's share of the .NET cost is therefore bounded
(EF Core costs about 20% more CPU per request than hand-written ADO.NET at the
same rate, and Dapper sits within 2% of raw ADO), and the prepared-statement
asymmetry documented in `docs/FAIRNESS.md` is quantified rather than merely
disclosed (EF Core knee 1,468 rps with Npgsql's default `Max Auto Prepare=0`
against 10,426 rps at `=20`).

## The capacity gate

The gate runs first and exists to prove the DB is not the thing being measured:
drive a create-only ladder until PG's own knee appears, project the mixed
workload's DB load, and require a `headroom_factor` of 1.3x of margin. If the
margin is missing it fires the pre-registered levers in order (write share
0.15 to 0.10, constructed cache hit rate 0.80 to 0.90, PG cores 3 to 4 taken from
vegeta), never `synchronous_commit=off`. A non-passing verdict aborts the
measured cells unless the operator sets `LEDGERBENCH_ALLOW_DB_IMPOSED_KNEE=1`,
which stamps the run `degraded` in the ledger and manifest.

The measured run's verdict was **`pass`**, and the shape of that pass matters:

1. **The PG figure is a floor, not a ceiling.** `pg_knee.json` records
   `knee_rps` 3,715 over 12 usable rungs of 12 at 3 PG cores with
   `lower_bound: true` and the detail "ladder never broke (every rung tracked)".
   PostgreSQL was still keeping up when the ladder ran out, so its real capacity
   is at least that.
2. **No levers fired.** `effective_workload` equals the frozen spec workload:
   15% write share, 0.80 constructed hit rate, 3 PG cores, `levers_applied: []`.
3. **The margin was met against a planning estimate, not against the cells.**
   The gate compared 3,715 rps to 2,160 rps of projected demand for a 1.72x
   margin where the pre-registered rule requires 1.3. That 2,160 is computed in
   `phases.py` as `ladder_start_rps` (500) x 8, an assumed SUT knee of 4,000 rps,
   times the 0.54 share of the mix that touches PostgreSQL, and it is fixed
   before the ladder runs. The cells then operated at 9,244, 11,093 and 13,312
   rps, implying roughly 5,000 to 7,200 rps of mixed DB load at the same 0.54
   fraction. **Errata:** the headroom rule as pre-registered evaluates a
   projection, not the realized operating rates, and the projection was stale by
   the time the cells ran. A future revision should re-evaluate headroom against
   the confirm rates the fit selects.

What that does and does not license: the gate establishes that PostgreSQL sustained
at least 3,715 create-only rps on 3 cores without breaking, on the heaviest DB path
in the mix, and the cells never drove it to a break either. It does **not**
establish that PostgreSQL was tested at the cells' realized rates, and it is not an
attribution of where a supra-knee service's time goes, because the
DB-CPU-per-request column that would settle that was descoped and not captured.
The comparison between the two stacks is internally valid either way: both faced
the identical DB under identical load, interleaved in time.

## Working set: the seed is not the workload

The full-scale dataset is 1M customers / 5M invoices. The **measured working set
is smaller**: the vegeta targets file holds 1,000,000 **requests**
(`LEDGERBENCH_TARGET_COUNT`, recorded in `manifest.working_set` as counted from
the generated file), which covers **137,028 distinct invoice ids and 112,014
distinct customer ids**. The file replays in order, so its cycle period at the
measured operating rates is 108 s at 9,244 rps, 90 s at 11,093 and 75 s at
13,312, all inside the 300 s cache TTL (`spec/cache.md`). The read path is
therefore somewhat more cache-resident than the constructed 0.80 hit rate
implies, and that residual is disclosed rather than corrected.

What is no longer inferred: the realized hit rate is measured per window from
Redis INFO deltas, and came out at **0.807** over the run, against the 0.80 the
workload was constructed to target. Drawing targets per-probe instead of
replaying a fixed file is the remaining fix; `workload.rng` already makes that
deterministic.

One disclosed drift: create traffic grows the invoice table over the run, so
the DB is not byte-identical between early and late probes. Interleaved
randomized blocks pair Go and .NET adjacently in time, which keeps the
headline ratio drift-immune; absolute knee rates carry this caveat.

## Coverage of the measured run

| specified | captured |
|---|---|
| CO validation suite, oha tail cross-check | not run; descoped at scope freeze; achieved==offered only |
| logging-sink microbench + no-drop test | not run; descoped |
| connection sweep | not run; descoped; pool pinned 24/24, manifest-asserted and verified warm 24/24 in every cell |
| capacity gate with headroom | ran, **passed**; PG floor 3,715 rps over 12/12 usable rungs, 1.72x margin, no levers |
| warmup gates `hot_set_touched`, `major_faults_asserted_equal` | declared, not implemented |
| under-warmed probes excluded from fits/confirms | recorded per probe and applied by `analyze`; flagging is heavy (59.5%) and language-asymmetric (Go 67.3% vs .NET 51.8%); one whole cell is posture-only |
| per-language knee fits | the live fit is pooled (and picked the confirm rates); per-language refits published, brackets thinner than the pre-registered rep minimum; Go never crossed, so Go has no knee |
| hysteresis, both ladder directions | ascending only |
| DB / vegeta / dockerd cgroup deltas | not captured (SUT containers only) |
| `/runtime-stats` GC counters per window | polled live, not flushed to disk |
| cache hit-rate telemetry | captured: realized 0.807 over 618 windows |
| p999 appendix with order-statistic CIs | not produced |
| data-layer variants (ado, dapper, prepared parity) | all three ran as full cells |
| isolation runs, cold curves, quota/boost/mem-sweep | not run; descoped |
| equivalence suite, stationarity, paired-difference CI, throttle/OOM zero, achieved==offered | ran and published |

Run totals for reference: 618 probe windows, 358,673,386 requests, status codes
`{200: 304,126,176, 201: 53,640,968, 500: 7, transport-level failures: 906,235}`.
The fourth entry is vegeta status code 0: connection-level failures with no HTTP
response, concentrated at supra-knee rungs, and the run-wide `errors.rate` of
0.0025 in `manifest.json`. The pre-registered error-rate gate excludes the probes
they occur in from every fit, so they are reported here and excluded there.
