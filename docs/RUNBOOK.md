# RUNBOOK: operating a measured Ledgerline run

Operational guide for the measured tier. The published run used a Ryzen 9 7950X on
Fedora 43, and the Tier-1 tuning steps are written against that box; a different
Linux host runs the same procedure with the Tier-2 steps detecting and skipping
what does not apply. Everything here is
Linux/Fedora only: the measured tiers refuse to run on Windows. All `ledgerbench` commands
run from `bench/`; `make` targets run from the repo root.

## Prerequisites

- **Docker: NATIVE engine** (rootful; cgroup v2 unified) running on the host.
  Docker Desktop does NOT qualify: its linuxkit VM hides container cgroups/PIDs
  from the host, so the headline CPU/memory sampling reads nothing and cpusets
  pin to vCPUs. Install `docker-ce` (or `moby-engine`), `systemctl enable --now
  docker`, and `docker context use default`. `ledgerbench doctor` enforces this
  (`docker_native_engine`).
- **uv** (Python runner) on PATH.
- **Root or NOPASSWD sudo** for the operator: host tuning (`host-setup.sh --tune`)
  re-runs at every `run-all` start (and on resume after a reboot) and needs root;
  the runner invokes it via `sudo -n`, which must not prompt. `ledgerbench doctor`
  checks this (`root_or_sudo`).
- **No foreign workloads**: stop unrelated containers/compose projects before a
  measured run; they sit outside the benchmark cgroups but compete for cores,
  LLC, and memory bandwidth.
- **make** (the targets are thin wrappers over `uv run ledgerbench …` / `docker compose …`).
- **Host tuning** applied via `make host-setup` (runs `infra/host-setup.sh --tune`). What it
  changes, all reversible and recorded per-action as JSON into a state file:
  - CPU governor → performance; turbo **boost off**; shallow C-states.
  - THP → madvise; IRQ steering off the SUT cores.
  - A systemd `CPUAffinity` drop-in pinning all host daemons (dockerd, containerd-shims,
    journald, systemd) to the CCD1 housekeeping core, so they stay off the SUT cores.
  - PGDATA on btrfs: `chattr +C` (CoW off) + `noatime`.
  - Stops `tuned`, `power-profiles-daemon`, `irqbalance`.
  - Tier-1 steps are exact-to-this-hardware; Tier-2 steps detect-and-skip on a different box
    and are tagged as such in the run manifest.
  - **Restore** with `make host-restore` (replays the state file to undo every mutation).
    A reboot also resets the live knobs (governor/boost/pinning); resume re-applies them.

## Full run sequence

Run these from the repo root, in order:

1. `make doctor`: preflight: docker, cgroup v2, free cores, disk, binaries, registry.
2. `make seed-full`: seed 1M customers / 5M invoices via the dockerized seeder
   (deterministic, seed 42, binary COPY, invariant-checked). Budget roughly an hour.
3. `make host-setup`: apply Tier-1 host tuning (see above).
4. `make bench-all`; the full measured run: capacity gate → validation (equivalence) →
   GC-posture headline cells → selected variants → soak. Budget ~15 h per cell
   (2 headline + any `LEDGERBENCH_RUN_VARIANTS` cells) plus ~2 h of gates/seed.
5. `make analyze RUN=<run_id>`: isotonic knee fits, paired-difference stats, CIs from
   `results/<run_id>/`, written to `results/<run_id>/report/`. Never hand-edit
   `report.md`; re-run this instead.
6. `make figures` (optional): redraw `docs/figures/*.png` from the committed artifacts.

**Check the phase verdicts before you trust the report.** The runner **aborts** the
measured cells on a non-passing capacity gate (`phases._enforce_gate`; opt out with
`LEDGERBENCH_ALLOW_DB_IMPOSED_KNEE=1`, see the env vars below), and `analyze` excludes
under-warmed probes itself. Neither of those replaces reading the verdicts: a gate can
pass and a cell can still be posture-only. Before publishing anything from a run, read:

- `results/<run_id>/capacity-gate/verdict.json`: a `verdict` of `db-imposed-shared-knee`
  means the gate exhausted its levers without reaching headroom. On a non-passing verdict
  the levers are deliberately **not** applied (a partly-moved workload matches neither the
  spec mix nor a headroom-clean one) and the run stops; the verdict also carries
  `levers_applied` / `levers_not_applied`, and the manifest carries `degraded` plus
  `degraded_allowed_by`. `rungs: []` with a `pg_knee_rps` that is exactly the first rung's
  achieved rate scaled by the core ratio means the create-only ladder never located a knee.
  A healthy verdict looks like the committed run's: `pass`, `usable_rungs` equal to
  `total_rungs`, `levers_fired: []`, and `pg_knee_is_lower_bound: true` because the ladder
  tracked every rung without breaking.
- `results/<run_id>/ledger.json` → `phases.validation`: equivalence is the gate.
  (The once-planned CO suite / logging-sink / connection-sweep / oha checks were
  descoped at scope freeze and do not appear.)
- `results/<run_id>/ledger.json` → per-cell `under_warmed` maps: probes flagged here are
  excluded from fits, confirms and the resource roll-up: `analyze` filters them and
  prints the used-vs-flagged counts per row, marking rows that had no measurement-grade
  window at all as posture-only (`*`). Flag counts are heavy and language-asymmetric in
  practice; `results/README.md` carries the committed run's per-cell counts as a worked
  example.
- `results/<run_id>/manifest.json` → `knobs_go` / `knobs_dotnet` are written once per run,
  not per cell, so they describe the last cell that ran, not the headline cell. Read
  `knobs_by_cell` instead, which records both languages' knobs per cell id.

Smoke rehearsal on any OS (measures nothing): `make smoke`.

### Operator environment variables

These knobs live in the operator's environment rather than in `spec/`, because `spec/` is
frozen at run start and no number in it may be invented after the fact. All are read by
`bench/ledgerbench/phases.py`.

| env var | effect |
|---|---|
| `LEDGERBENCH_ALLOW_DB_IMPOSED_KNEE` | `1`/`true`/`yes`/`on` lifts the capacity-gate abort: the run proceeds on a non-passing verdict and is stamped `degraded: true` + `degraded_allowed_by` in both the ledger and `manifest.json`, so every downstream artifact carries the caveat. Unset (the default) = a non-passing gate stops the run. Use only when a degraded run is deliberate. The stamp is run state: once recorded, a resume replays it: a recovery shell does not need the variable re-exported (an UNstamped degraded gate keeps blocking on resume until the operator opts out, and that late opt-in is then stamped too). |
| `LEDGERBENCH_CACHE_TTL_SECONDS` | Integer seconds. The gate's `cache_hit_rate` lever is realized (per `spec/cache.md`) as a raise of `CACHE_TTL_SECONDS`, but `spec/slo.yaml` pre-registers no TTL value. Without this var the lever is recorded **not applied**: which fails the gate rather than letting a lever count as fired while nothing changed. Set it to make that lever real; it is pushed to the services as compose env alongside the cpuset move. It has no effect on a run where that lever never fires. |
| `LEDGERBENCH_TARGET_COUNT` | Integer ≥ 1000, **default 1000000**, which is what the committed run used (137,028 distinct invoices, realized hit rate 0.807). Requests in the generated `--scale full` targets file: the working-set dial. Lowering it shrinks the working set and makes the read path more cache-resident, which changes what the run measures rather than just how long it takes. Bench-side dial: no `spec/` edit, no `config_hash` change. Pinned into the ledger at run start; on resume the pin wins over the environment (a mismatch prints a NOTE), so set it BEFORE starting the run. |
| `LEDGERBENCH_RUN_VARIANTS` | Comma-separated variant cell ids to EXECUTE in the variants phase (e.g. `variant.ado,variant.dapper`); unknown ids abort. Default: variants are declared but not run. The selection is validated and pinned into the ledger at run start (before any phase runs), so a resume, including the documented naked-shell recovery, replays the original selection regardless of the environment. Each executed variant is a full cell (ladder→fit→confirm→soak, ~15 h at full scale). |

## Unattended operation

The run spans ~3 nights with no operator present. **A UPS is strongly recommended.** A power
cut is not catastrophic but it is not free: you lose the **in-flight probe** plus **one redone
interleave block** (the runner redoes any interleave block split by the crash to preserve
pair integrity), roughly **20-30 minutes** of work. Everything completed before that is
preserved by the ledger.

### Recovery after a power cut / reboot

1. **Start docker** (`systemctl start docker` if it is not set to start on boot).
2. **`cd bench`**.
3. **`uv run ledgerbench status`**: see which phase/cell/probe was in flight and what is
   pending next.
4. **`uv run ledgerbench run-all --scale full --resume`**: resumes the most recent unfinished
   run. Host re-tuning (the reboot reset governor/boost/CPU pinning) and DB invariant checks
   run automatically as part of the resume protocol; doctor is re-run; vegeta targets are
   re-shipped. To resume a specific run instead: `… --resume --run-id <ID>`.
5. **Verify** with `uv run ledgerbench status` that the redone interleave block shows up
   (the split block is re-queued and re-run, not skipped).

Equivalent make targets: `make bench-status` (status, any OS) and `make bench-resume`
(resume, Linux-gated).

## What the ledger guarantees

State lives in `results/<run_id>/ledger.json`, written atomically at probe granularity.

- **Done probes never re-run.** A completed probe is recorded and skipped on resume; only the
  in-flight probe and the crash-split interleave block are redone.
- **config_hash freeze.** The ledger pins a hash of `spec/` at run start. Resume refuses if
  `spec/` changed; the measured matrix must stay constant across the whole run.
- **Artifacts checksummed before a probe counts.** A probe's outputs (histograms, cgroup CSVs,
  GC/pool tables) are written and checksummed before the ledger marks the probe done, so a
  crash mid-write can never leave a half-written artifact counted as complete.

## Troubleshooting

- **status says `config_hash mismatch`** → `spec/` was edited after the run started. The run
  cannot resume against a changed matrix. Start a fresh run (`make bench-all`); do not edit
  `spec/` mid-run.
- **vegeta targets missing after reboot** → expected and harmless; they are re-shipped
  automatically by the resume protocol. No manual step.
- **docker volumes after reboot** → volumes survive a reboot. PGDATA is crash-safe with
  `fsync=on` / `synchronous_commit=on`; on resume the DB invariants are re-verified
  (counts, balance = sum of items) before any probe records.
- **host knobs reset after reboot** (governor back to default, boost back on, pinning gone) →
  expected; resume re-applies host tuning automatically. To apply manually: `make host-setup`.
- **doctor fails on resume** → resume re-runs doctor by design; fix what it reports (docker
  down, a core busy, disk full) and re-run the resume command.
- **capacity gate says `db-imposed-shared-knee`** → the gate could not find 1.3× headroom
  even after its levers, and **the run aborts** rather than measure the cells against a DB
  whose headroom was never established. Options: give PG more cores in the compose overlay (`PG_CPUS`; the default
  in `compose.bench.yaml` is 3 cores; the gate's `pg_cores` lever now emits the `PG_CPUS`
  / `VEGETA_CPUS` move itself and the runner re-creates the containers on the new cpusets,
  but only on a run where the lever makes the gate pass), lower the create share in
  `spec/workload.yaml` *before* starting a run (never mid-run: `config_hash`), or re-run
  with `LEDGERBENCH_ALLOW_DB_IMPOSED_KNEE=1` and publish a run stamped `degraded`
  alongside the verdict. Do not reach for `synchronous_commit=off`; it is pre-registered
  as never.
- **manifest fields come back null** (`host_state`, `topology`, `postgres_config`,
  `identity.git_sha`) → the tuning and topology were applied but not read back into the
  manifest. Nothing to fix mid-run; treat the affected provenance as unevidenced for that
  run and say so when publishing.
