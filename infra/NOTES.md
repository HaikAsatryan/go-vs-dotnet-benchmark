# infra/: decisions and divergence notes

Scope: the compose topology, the vegeta load-generator image, and the Fedora host
scripts. Records the non-obvious infra choices and where they diverge from the
obvious default. Conflicts resolve toward the LOWEST cross-language divergence
risk; `spec/` is the source of truth for any contract.

## Image tags (verified via `docker manifest inspect`, read-only)

| image | tag chosen | verified | digest-pin |
|---|---|---|---|
| postgres | `postgres:18.4-bookworm` | EXISTS | pinned |
| redis | `redis:8.10.0-trixie` | EXISTS (latest stable; 8.5+ ships trixie-based images only, no bookworm tags) | pinned |
| golang (vegeta build) | `golang:1.26.5-bookworm` | EXISTS | pinned |
| debian (vegeta runtime) | `debian:bookworm-slim` | EXISTS | pinned |
| prometheus / grafana / cadvisor | pinned tags, EXPLORATORY ONLY | not load-bearing | pinned |

Redis 8.10.0 is the newest stable tag (trixie-based; upstream stopped publishing
bookworm variants after 8.4). Valkey is documented as a drop-in in the project README
(spec/cache.md requirement); not used here.

All `FROM`/`image:` references carry an `@sha256:` digest pin so a tag re-point
upstream can never silently change what runs. Re-resolve digests deliberately,
never implicitly.

## Load-generator image

(oha and its never-run tail cross-check were removed in the scope freeze;
the image now ships vegeta only.)

vegeta itself is built from source via `go install github.com/tsenart/vegeta/v12@v12.13.0`
in the locked Go toolchain, so its version is reproducible regardless of release
artifacts.

## Compose topology

- Build contexts are relative to `infra/` (the compose file's dir), so `..` ==
  repo root and `../spec/...` reaches the spec mounts. Compose is always invoked
  from repo root (`-f infra/compose.yaml ...`), matching the Makefile.
- Profiles: postgres + redis have NO profile (always start). `go`, `dotnet`,
  `load`, `seed` gate the rest. The runner starts exactly ONE SUT profile per
  run, so the two SUTs are never co-resident.
- Port map (host->container): postgres 54320->5432, redis 63790->6379,
  sut-go 8081->8080, sut-dotnet 8082->8080. SUTs on distinct host ports so the
  smoke path can run them sequentially without clashes.
- Identical docker `json-file` logging (max-size 512m, max-file 3) on BOTH SUTs
  and every infra service (spec/logging.md: identical compose log config).
- Per-SUT pdf volumes (`pdfstub-go`, `pdfstub-dotnet`) so the pdf-stub write
  target is isolated per language and recreated per run.
- Env passthrough: every spec env var has a default matching spec
  (DB_POOL_MAX=24, DB_POOL_MIN=24, DB_CONN_LIFETIME=3600, REDIS_POOL_MAX=16,
  CACHE_TTL_SECONDS=300, LOG_QUEUE_LEN=8192, BODY_MAX_BYTES=65536,
  MAX_AUTO_PREPARE=0, DATA_LAYER=efcore, GOGC=100, GOMEMLIMIT= (empty),
  DOTNET_GCDynamicAdaptationMode=1, PDF_DIR=/data/pdf). The runner overrides per
  cell; defaults are the headline values. On the bench overlay GOMAXPROCS /
  DOTNET_PROCESSOR_COUNT are pinned to the SUT core count and the runner asserts
  the SUT's reported processor_count from `/runtime-stats`.

## SUT liveness probe: a `/healthcheck` helper binary

Neither the Go SUT base (scratch/distroless) nor the ASP.NET base ships `curl`.
The only cross-language-identical liveness probe is for EACH SUT image to ship a
tiny `/healthcheck <url>` helper that GETs `/healthz` (the static HTTP floor,
excluded from access logging per spec/logging.md) and exits 0/non-0. Chosen over
curl/wget because it is identical across both base images (zero divergence) and
needs no extra package install.

In the base compose the SUTs carry NO container healthcheck: the runner polls
`/readyz` over the published port before any traffic (identical convention for
both SUTs, keeps the images minimal). A SUT image may instead bake its own
`HEALTHCHECK` using the `/healthcheck` helper; `--wait` works either way.

## Connection-string env names

The compose passes `DATABASE_URL` (pgx URL form for Go; Npgsql keyword form for
.NET) and `REDIS_ADDR` (`host:port`). The exact string SHAPE differs by driver
(unavoidable: pgx wants a URL, Npgsql wants a keyword string), so the value is
defaulted per SUT (`GO_DATABASE_URL` vs `DOTNET_DATABASE_URL`) while the env var
NAME (`DATABASE_URL`) is identical across both languages. The seeder uses the
superuser URL (`SEED_DATABASE_URL`) because it needs DDL + COPY + sequence
restart + grants (spec/db-access.md seeder contract); the SUTs use the
least-privilege `ledger` role (spec/postgres/initdb/01-role.sql).

## compose.bench.yaml core ids are PLACEHOLDERS

cpuset ids (SUT 0-5, PG 8-10, vegeta 11-13, redis 14, housekeeping 15) assume
CCD0 = cores 0-7, CCD1 = cores 8-15 on the 7950X with SMT siblings idle. The
REAL CCD-to-logical-core mapping is finalized on the Fedora box (read from
`/sys/devices/system/cpu/cpu*/topology`) and echoed to the manifest. All cpusets
are env-parameterized so finalizing them is an env edit, never a file edit. The
capacity-gate core-rebalance lever (vegeta core -> PG, used if PG becomes the
capacity gate) is the same env edit.

## Overlay model

- `compose.bench.yaml`: cpuset + mem_limit only. NO `cpus:` quota: a CFS `cpus:`
  quota throttles in fixed periods and injects p99 stalls, which cpuset pinning
  avoids.
  (The once-planned `compose.bench.quota.yaml` CFS-quota overlay and
  `compose.co.yaml` toxiproxy overlay were removed in the scope freeze;
  their variants/checks will not run.)
- `compose.observability.yaml`: prom+grafana+cadvisor stub, EXPLORATORY ONLY,
  NEVER a recorded data source (cgroup v2 is the only source). Big warning
  banner in the file.

## Host scripts (Fedora-only)

- `host-setup.sh` / `host-restore.sh`: bash, LF, idempotent, `--tune`-gated
  (no `--tune` = detect-and-report only, zero mutation). Every action emits one
  JSON line for the manifest. `--isolate` only CHECKS the kernel cmdline
  (isolcpus/nohz_full need a reboot; never edited live).
- Reversal is driven by a JSON-lines state file (`/var/tmp/ledgerline-host-state.json`,
  override `LEDGER_HOST_STATE`); restore replays it in reverse and clears it
  (idempotent). Tier-1 (exact) vs Tier-2 (directional, detect-and-skip) recorded
  per action via the `tier` field.
- Covered: governor->performance, boost off (cpufreq/boost or amd_pstate
  per-policy), shallow C-states (cpupower idle-set), THP->madvise, IRQ steering
  off SUT cores, global systemd `CPUAffinity` drop-in (the key host-hygiene fix:
  pins dockerd/shims/journald/systemd to the housekeeping core so container
  cpusets are not undermined by host daemons), pgdata chattr +C + noatime report,
  conflicting-service stops (tuned, power-profiles-daemon, irqbalance).
- noatime and isolcpus are detect-and-report (live remount of a docker volume's
  backing fs and live bootloader edits are too intrusive; left to documented
  manual/fstab/reboot steps). Recorded as Tier-2/checked.

## Status / deferred to the measured run

Compose `config` parses cleanly for all surviving overlay combinations (base;
+bench; +observability; +bench+observability), resolved cpuset/mem_limit match
the resource ledger, profile gating is correct (default = postgres+redis only;
one SUT per profile), and the postgres/prometheus binds resolve to the real
spec files (read-only). (The quota and CO overlays, once verified here too,
were deleted in the scope freeze: see "Overlay model" above.) Both host
scripts are `bash -n` syntax-clean; their detection-mode output is valid JSON
and the non-Linux platform guard exits 0.

Resolved on the measured Linux run (not on a dev box):

- Real CCD core-id mapping read from the Fedora box topology.
- Actually executing host-setup/host-restore (Fedora-only; Linux-gated).
