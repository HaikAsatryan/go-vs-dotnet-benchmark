# Ledgerline: Go vs .NET benchmark. Targets are thin wrappers; PowerShell users
# can run the underlying `uv run ...` / `docker compose ...` commands directly.
# Linux-only targets (bench-smoke, bench-all, host-setup, host-restore) guard on
# uname and refuse elsewhere. Windows users invoke make via Git Bash / WSL, so
# `uname -s` exists there too.

UV      ?= uv
COMPOSE ?= docker compose

# DSN the dockerized seeder uses to reach the compose-network postgres.
SEED_DSN ?= postgres://postgres:postgres@postgres:5432/ledgerline

.PHONY: doctor smoke bench-smoke bench-all bench-resume bench-status seed-smoke \
        seed-full analyze figures watch regen regen-go regen-dotnet lint \
        fmt host-setup host-restore

doctor:               ## preflight checks (per-OS subset on Windows)
	cd bench && $(UV) run ledgerbench doctor

smoke:                ## functional tier: build, seed smoke, equivalence, short probe (Windows-ok)
	cd bench && $(UV) run ledgerbench smoke

bench-smoke:          ## Linux-only: full measured pipeline at smoke scale (one cell, fast)
	@if [ "$$(uname -s)" != "Linux" ]; then echo "Linux-only target (needs cgroup v2 + host tuning)."; exit 1; fi
	cd bench && $(UV) run ledgerbench run-all --scale smoke

bench-all:            ## Linux-only: full measured run (capacity gate -> validation -> cells -> variants)
	@if [ "$$(uname -s)" != "Linux" ]; then echo "Linux-only target (needs cgroup v2 + host tuning)."; exit 1; fi
	cd bench && $(UV) run ledgerbench run-all --scale full

bench-resume:         ## Linux-only: resume the most recent unfinished measured run after an interruption
	@if [ "$$(uname -s)" != "Linux" ]; then echo "Linux-only target (needs cgroup v2 + host tuning)."; exit 1; fi
	cd bench && $(UV) run ledgerbench run-all --scale full --resume

bench-status:         ## latest run's ledger: phase states, per-cell probe progress, next pending item
	cd bench && $(UV) run ledgerbench status

seed-smoke:           ## seed a tiny dataset via the dockerized seeder (compose 'seed' profile)
	$(COMPOSE) -f infra/compose.yaml --profile seed run --rm seed \
	  --dsn $(SEED_DSN) --scale smoke --seed 42 --schema /spec/schema.sql --grants --truncate

seed-full:            ## seed the full 1M/5M dataset via the dockerized seeder
	$(COMPOSE) -f infra/compose.yaml --profile seed run --rm seed \
	  --dsn $(SEED_DSN) --scale full --seed 42 --schema /spec/schema.sql --grants --truncate

analyze:              ## fits, stats, CIs for a run id: make analyze RUN=<run_id>
	cd bench && $(UV) run ledgerbench analyze $(RUN)

figures:              ## regenerate docs/figures/*.png from the committed run artifacts
	cd bench && $(UV) run --with matplotlib python figures.py

watch:                ## exploratory observability stack (NEVER the recorded source)
	$(COMPOSE) -f infra/compose.yaml -f infra/compose.observability.yaml up -d prometheus grafana cadvisor

regen: regen-go regen-dotnet  ## regenerate committed codegen + freshness check

regen-go:             ## regenerate sqlc output; fails if the committed tree is stale
	cd services/go && sqlc generate && git diff --exit-code internal/db/gen

regen-dotnet:
	cd services/dotnet && dotnet ef dbcontext optimize --output-dir Data/CompiledModels --namespace Ledgerline.Data.CompiledModels

fmt:                  ## format Go + C# in place (Python is linted, not formatted: see pyproject)
	cd services/go && gofmt -w . && cd ../../seed && gofmt -w .
	cd services/dotnet && dotnet format

lint:                 ## check formatting + lint without writing: what CI runs
	@out="$$(gofmt -l services/go seed)"; if [ -n "$$out" ]; then echo "gofmt needed:"; echo "$$out"; exit 1; fi
	cd services/dotnet && dotnet format --verify-no-changes
	cd bench && $(UV) run ruff check .
	cd services/go && go vet ./...
	cd seed && go vet ./...

host-setup:           ## Fedora-only host tuning (gated; see infra/host-setup.sh)
	@if [ "$$(uname -s)" != "Linux" ]; then echo "Linux-only target (host tuning)."; exit 1; fi
	@if [ "$$(id -u)" = "0" ]; then bash infra/host-setup.sh --tune; else sudo -n bash infra/host-setup.sh --tune; fi

host-restore:
	@if [ "$$(uname -s)" != "Linux" ]; then echo "Linux-only target (host restore)."; exit 1; fi
	@if [ "$$(id -u)" = "0" ]; then bash infra/host-restore.sh; else sudo -n bash infra/host-restore.sh; fi
