# results/

Per-run artifacts land here as `results/<run_id>/`:

- `ledger.json`: crash-safe run state (phases, per-cell probe progress); drives `--resume`.
- `manifest.json`: full run provenance (versions, knobs, capacity gate, config hash).
- `CHECKSUMS.committed.sha256`: sha256 over exactly the files git tracks in the run,
  deduplicated, including `manifest.json`, `ledger.json`, `doctor.json` and `report/`.
  This is the one to verify against.
- `CHECKSUMS.run.sha256`: the runner's own append-only log, kept verbatim as run
  evidence. A probe only counts once its outputs are checksummed. It is **not** a
  usable verification file: it covers the gitignored `.csv` traces (1,272 paths not
  in this repo), 108 paths carry two hashes because the probe was redone on resume,
  and it never covered the six roll-up artifacts above.
- `capacity-gate/`: the PostgreSQL capacity ladder and its verdict, run before any cell.
- `cells/`: per-cell probe summaries (rung stats, warmup gates, cgroup CPU/memory deltas).
- `report/`: analysis output tables (`make analyze RUN=<run_id>`).

Small text/JSON summaries are committed. Smoke and rehearsal runs are not committed.

**The raw per-window traces are not published.** The cgroup 50 ms cpu/mem CSVs, the
per-second p99 series, the raw vegeta `.bin` captures and the full DB dumps come to about
6.3 GB per run, and no GitHub Release carries them today. Everything needed to re-verify
every published number is in git: `ledger.json`, `manifest.json`, the per-probe
`*.summary.json` / `*.warmup.json` roll-ups, `capacity-gate/`, `report/` and
`CHECKSUMS.committed.sha256`. The raw traces add per-window detail behind those roll-ups, and they
are regenerable by re-running the tier. If you need them for a specific claim, open an
issue and say which probe.

## The committed run: `20260814T051533-fedora`

Full scale, Tier-1 tuned, config hash `7889756b…`. Five cells, **618 probe windows,
358,673,386 requests**, status codes `{200: 304,126,176, 201: 53,640,968, 500: 7,
transport-level failures: 906,235}`. Those 906,235 are vegeta status code 0, load-generator
connection errors concentrated at supra-knee rungs; they are the run-wide `errors.rate`
of 0.0025 in `manifest.json`, and the pre-registered error-rate gate excludes the probes
they occur in from every fit.

Provenance is anchored on the `config_hash`, which recomputes from the committed `spec/`:

```sh
cd bench && uv run python -c "from ledgerbench.config import config_hash; print(config_hash())"
# 7889756b00193358a813ff55c42f3e1e806a7cee3bc5d26b65049420b0ec9aaf
```

`spec/` is frozen for exactly this reason: `config_hash` covers every byte of every file
under it, including the `.md` contracts, so a one-character edit anywhere in `spec/`
breaks the recomputation above. Errata against the spec go in
[`../docs/METHODOLOGY.md`](../docs/METHODOLOGY.md), never into the file.

The committed artifacts verify with:

```sh
cd results/20260814T051533-fedora && sha256sum -c CHECKSUMS.committed.sha256
# 2479 files, all OK
```

`manifest.json` also records `identity.git_sha` (`458cd71…`). That commit belonged to the
pre-publication history, which was reset when this repo was opened, so the SHA no longer
resolves. The config hash and `CHECKSUMS.committed.sha256` are what replace it.

| cell | .NET data layer | ladder | confirm rates | soak |
|---|---|---|---|---|
| `headline.mixed.gc-default` | EF Core, defaults | 7 rungs, 500 to 1,493 rps | 1,244 · 1,493 | 1,244 |
| `headline.mixed.gc-generous` | EF Core, defaults | 6 rungs, 500 to 1,244 rps | 1,037 · 1,244 | 1,037 |
| `variant.ado` | raw ADO.NET | 18 rungs, 500 to 11,093 rps | 9,244 · 11,093 | 9,244 |
| `variant.dapper` | Dapper | 18 rungs, 500 to 11,093 rps | 9,244 · 11,093 | 9,244 |
| `variant.prepared-parity` | EF Core + `Max Auto Prepare=20` | 19 rungs, 500 to 13,312 rps | 11,093 · 13,312 | 11,093 |

Every rung is 3 interleaved go/.NET blocks; every confirm rate is 10
randomized-complete-block reps per language; a block split by an interruption is
redone whole so the pairing stays honest.

**The run was interrupted and resumed.** Probe timestamps span 89.2 hours of
wall-clock across four days (2026-08-14 05:54 UTC to 2026-08-17 23:06 UTC), but the
steady probe cadence is 6.1 minutes and five gaps break it: one of **20.7 hours** in
the middle of the `variant.prepared-parity` ladder (at rate 2,580, between blocks 0
and 1), and four of 100, 42, 42 and 42 minutes. About **65 hours** of that span was
actually driving load. The long gap was a resume under the documented protocol: the
ledger shows the `host_setup` phase re-running at 2026-08-17T12:17:20Z, five minutes
before the first probe after the gap, so host tuning was re-applied and the split
interleave block was redone whole.

**Knees at the 20 ms promise** (per-language refits of the same ladder rungs):
.NET 1,468 (EF defaults), 10,177 (Dapper), 10,334 (raw ADO.NET), 10,426 (EF +
prepared). Go: **no crossing in any cell**, so its knees are lower bounds set by
the top rung where warm Go windows exist, not walls Go hit.

**Cost at the operating rate**, request-weighted over warm confirm + soak
windows: .NET spent 2.81x (ADO, 9,244 rps), 2.88x (Dapper, 9,244) and 3.09x
(prepared EF, 11,093) Go's CPU-ms per request, and 3.10x / 3.42x / 5.33x its
in-window anon memory p99. In the `gc-default` cell at 1,244 rps the CPU ratio is
4.65x. The ratio is a curve, not a constant: see the CPU-vs-rate section of
`report/report.md` for the per-rung values.

**The capacity gate passed.** `capacity-gate/verdict.json` records `pass` with
`pg_knee_rps` 3,715 over 12 usable rungs at 3 PG cores, and
`pg_knee_is_lower_bound: true` because the create-only ladder never broke, so
that figure is a floor on PostgreSQL's capacity rather than a ceiling. No levers
fired, and the effective workload equals the frozen spec workload (15% write
share, 0.80 target hit rate, 3 PG cores).

**But read the headroom test for what it is.** The gate compared 3,715 rps against
2,160 rps of *projected* demand for a 1.72x margin, and that 2,160 is a
pre-registered planning estimate computed before the ladder ran: 500 rps
(`ladder_start_rps`) x 8 as an assumed SUT knee of 4,000 rps, x the 0.54 fraction of
the mix that touches PostgreSQL. No cell operated at 4,000 rps. They operated at
9,244, 11,093 and 13,312, which at the same 0.54 fraction imply roughly 5,000 to
7,200 rps of mixed DB load. So the gate does **not** show PostgreSQL was tested at
the rates the cells later chose. What it does show is that PostgreSQL sustained at
least 3,715 rps of *create-only* traffic (the heaviest DB path in the mix, at 15% of
it) on 3 cores without breaking, and that it was never observed to break during the
cells either. Treat the database as unfalsified rather than as proven clear.

**Cache residency was measured, not assumed.** `manifest.cache_stats` records
86,612,237 keyspace hits against 20,765,032 misses over the 618 windows, a
realized hit rate of **0.807** against the 0.80 the workload was constructed to
target.

### Caveats that travel with these numbers

- **Go's knee was never located.** Go did not cross 20 ms inside any ladder, so
  every Go capacity figure is a lower bound. At the highest rate the run offered,
  13,312 rps, Go's warm windows averaged a 16.3 ms p99 with a worst window of
  20.4 ms.
- **Under-warmed flagging is heavy and language-asymmetric.** The warmup verdict
  is recorded per probe in `ledger.json`; `analyze` excludes flagged probes from
  every fit and roll-up and prints the counts per row. Totals: **368 of 618
  windows flagged (59.5%)**, Go 208/309 (67.3%), .NET 160/309 (51.8%).

  | cell | Go flagged | .NET flagged |
  |---|---|---|
  | `headline.mixed.gc-default` | 22/42 | 25/42 |
  | `headline.mixed.gc-generous` | 39/39 | 26/39 |
  | `variant.ado` | 50/75 | 36/75 |
  | `variant.dapper` | 51/75 | 41/75 |
  | `variant.prepared-parity` | 46/78 | 32/78 |

  Consequence: several Go roll-ups rest on one or two warm windows (the `ado`
  cell's Go CPU figure is a single window of 1,109,283 requests; the
  `prepared-parity` one is a single window of 1,331,174). The window counts are a
  row of every published table for exactly this reason.
- **`gc-generous` is posture-only.** All 39 Go windows are flagged, since
  `GOGC=off` cannot satisfy the `gc_steady` gate by construction, and 26 of the
  39 .NET windows are, with no declared cause. Because the Go side is 39/39, no
  interleave block has a warm window on both sides, so the cell yields no paired
  measurement. Its numbers are marked `*` and support one directional statement
  only: Go held 6,619 MB of
  anon memory (peak 6,820) against .NET's 212 MB (peak 503).
- **22 probes were dropped by the CO-safety and error-rate gates**, on top of the
  under-warmed flagging above and counted separately from it. These are the
  pre-registered measurement gates firing: `achieved/offered` outside +-2% (the
  load generator could not place the offered rate) or `error_rate` above the 0.1%
  SLO. 9 fell on the Go side and 13 on the .NET side, so the gate is not
  one-sided; 7 were error rate and 15 were rate fidelity. `analyze` excludes
  every one of them from every fit and roll-up, and `report/report.md` prints
  the reason beside each row's window count.

  | cell | stage | offered rps | language | block | reason |
  |---|---|---|---|---|---|
  | `variant.ado` | confirm | 9,244 | Go | block 0 | achieved/offered 0.9310 outside +/-2.0% |
  | `variant.ado` | confirm | 9,244 | Go | block 1 | achieved/offered 0.4828 outside +/-2.0% |
  | `variant.ado` | confirm | 11,093 | Go | block 0 | achieved/offered 0.9166 outside +/-2.0% |
  | `variant.ado` | confirm | 11,093 | Go | block 5 | achieved/offered 0.9071 outside +/-2.0% |
  | `variant.ado` | ladder | 11,093 | Go | block 0 | achieved/offered 0.6685 outside +/-2.0% |
  | `variant.dapper` | confirm | 11,093 | .NET | block 0 | achieved/offered 0.9648 outside +/-2.0% |
  | `variant.dapper` | confirm | 11,093 | Go | block 5 | achieved/offered 0.3988 outside +/-2.0% |
  | `variant.dapper` | ladder | 9,244 | .NET | block 1 | achieved/offered 0.9698 outside +/-2.0% |
  | `variant.prepared-parity` | confirm | 13,312 | .NET | block 0 | error_rate 0.0236 > 0.001 |
  | `variant.prepared-parity` | confirm | 13,312 | .NET | block 1 | achieved/offered 0.8152 outside +/-2.0% |
  | `variant.prepared-parity` | confirm | 13,312 | .NET | block 2 | error_rate 0.0024 > 0.001 |
  | `variant.prepared-parity` | confirm | 13,312 | .NET | block 3 | achieved/offered 0.9373 outside +/-2.0% |
  | `variant.prepared-parity` | confirm | 13,312 | .NET | block 4 | error_rate 0.0155 > 0.001 |
  | `variant.prepared-parity` | confirm | 13,312 | .NET | block 5 | achieved/offered 0.9794 outside +/-2.0% |
  | `variant.prepared-parity` | confirm | 13,312 | .NET | block 6 | error_rate 0.0251 > 0.001 |
  | `variant.prepared-parity` | confirm | 13,312 | .NET | block 7 | error_rate 0.0188 > 0.001 |
  | `variant.prepared-parity` | confirm | 13,312 | .NET | block 8 | achieved/offered 0.9071 outside +/-2.0% |
  | `variant.prepared-parity` | confirm | 13,312 | .NET | block 9 | error_rate 0.0112 > 0.001 |
  | `variant.prepared-parity` | confirm | 13,312 | Go | block 9 | achieved/offered 0.7732 outside +/-2.0% |
  | `variant.prepared-parity` | ladder | 9,244 | Go | block 1 | achieved/offered 0.8503 outside +/-2.0% |
  | `variant.prepared-parity` | ladder | 13,312 | .NET | block 2 | error_rate 0.0085 > 0.001 |
  | `variant.prepared-parity` | ladder | 13,312 | Go | block 0 | achieved/offered 0.5854 outside +/-2.0% |

  Ten of the thirteen .NET exclusions are the `prepared-parity` confirm at 13,312
  rps, where .NET was past its wall: up to 2.5% errors and as little as 81.5% of
  the offered load delivered. That row is posture for that reason, not because of
  the warm-up gates.
- **The knee intervals are not inferential CIs.** The bracketed interval is the
  2.5/97.5 percentile of 2,000 isotonic refits resampling the ladder's per-rung
  reps. The SLO-crossing bracket carries as few as 1 measurement-grade rep
  against the pre-registered minimum of 10 (`ladder.confirm_reps`), because the
  ladder runs 3 reps per rung and the under-warmed exclusion thins it further.
  Read them as spread indicators over a handful of reps.
- **Pooled knees are not either language's knee.** The runner brackets the
  crossing on p99 pooled over go and .NET probes, and that pooled value chose the
  confirm rates and the resource-cost rate filter. Where under-warmed exclusion
  leaves only one language in the fitted rungs (`gc-generous`), the report says so
  in the row. Always read the per-language refits.
- **Equal rate, not equal latency promise.** The resource tables compare both
  stacks at the same offered rate. At several of those rates Go is inside the
  20 ms SLO and .NET is not, so those rows are cost at equal offered load. The
  per-language SLO table in `report/report.md` prints each language's own p99
  against 20 ms, plus its worst window and how many windows individually met the
  promise.
- **Only one cell produced paired confirm verdicts** (`gc-default`, n = 2 and 3
  blocks). Both 95% paired-t intervals span zero, so direction and effect size
  only.
- **Provenance gaps in `manifest.json`.** `versions`, `host_state`, `topology`
  and `postgres_config` record `source: not_measured` with a note explaining that
  nothing reads them back off the box; host tuning is documented by
  `infra/host-setup.sh --tune` and the doctor phase rather than evidenced by the
  artifact. The `versions` block is all-null for the same reason: the versions in
  force are pinned in `go.mod`, `Ledgerline.csproj` and the digest-pinned
  Dockerfiles, and the authoritative table is in the root README, but nothing
  read them back off the running containers. `identity.git_sha` and `config_hash` are populated, and the errors,
  throttle, fault and cache blocks agree with the per-probe evidence.
- **Columns specified but not captured**: DB-CPU-per-request (only the two SUT
  containers are cgroup-sampled, so a wall cannot be split between app cost and
  query-plan cost), `/runtime-stats` GC counters per window, descending-ladder
  hysteresis, p999 order-statistic CIs, and the two declared warmup gates
  `hot_set_touched` and `major_faults_asserted_equal`, which are not implemented.
  `report/report.md` lists these under "Not captured in this run (scope)".

Which of these are method gaps and which are runner defects is worked through in
[`../docs/METHODOLOGY.md`](../docs/METHODOLOGY.md) and
[`../docs/FAIRNESS.md`](../docs/FAIRNESS.md).
