# services/go: implementation notes and decision log

Implementation notes for the Go SUT (Ledgerline, module `ledgerline/server`).
`spec/` is the single source of truth; the resolved decisions are recorded below.

## Dependency pins

Versions live in `go.mod` and the `Dockerfile`, which are the only places they are
stated: repeating them here is how they go stale. The README's version table lists
them alongside their source. Two choices that are not visible in `go.mod`:

- UUIDv4 is generated inline from `crypto/rand` (`internal/httpx/requestid.go`),
  so there is no `google/uuid` dependency.
- sqlc is a build-time tool only; the generated code under `internal/db/gen/` is
  COMMITTED, so the Docker image needs no sqlc toolchain.

## sqlc

- `sqlc.yaml`: schema `../../spec/schema.sql`, queries `../../spec/db-access.sql`,
  `sql_package: pgx/v5`, `emit_interface: true`, `bigint -> int64` override
  (money stays int64 end-to-end).
- `sqlc generate` was run; output committed (`db.go`, `models.go`, `querier.go`,
  `db-access.sql.go`). Regenerates cleanly with exit 0.

## POST /invoices transaction round-trip

Spec/db-access.md describes: `pool.Begin` + ONE `tx.SendBatch` carrying
`InsertInvoice` (RETURNING id+created_at) + N `InsertInvoiceItem` +
`UpdateCustomerBalance` + `Commit`.

CONSTRAINT: `invoices.id` is `GENERATED ALWAYS AS IDENTITY`, and the frozen
`InsertInvoiceItem` SQL binds `invoice_id` as `$1` (a parameter). pgx binds all
`pgx.Batch` parameters at `Queue()` time, BEFORE `SendBatch` runs, and pgx does
NOT propagate a RETURNING value from one batched statement into a later one
(unlike Npgsql's EF batch, which propagates the store-generated key within its
`NpgsqlBatch`). Therefore the literal "all writes including the header in ONE
pgx.Batch" is infeasible while keeping the frozen parameterized item SQL: the
item rows need the generated id, which is only known after the header insert.

RESOLUTION (lowest cross-language divergence): `internal/db/tx_create_invoice.go`
does `pool.Begin`, runs the header `INSERT ... RETURNING id, created_at` to obtain
the IDENTITY id (needed both to bind item FKs and to build the 201 body), then
pipelines the N item inserts + the balance update on ONE `pgx.Batch` via
`tx.SendBatch`, then `Commit`. This mirrors EF Core's ACTUAL behavior (principal
insert to get the store-generated key, dependents batched). The exact wire
round-trip count (BEGIN + header + batched-writes + COMMIT) is measured on the
measured run; the code is not contorted to hit a specific number.

The hand-copied batch SQL constants (`sqlInsertInvoice`, `sqlInsertInvoiceItem`,
`sqlUpdateCustomerBalance`) are asserted byte-for-byte against
`spec/db-access.sql` by `internal/db/sql_parity_test.go::TestSQLParity`.

## cache-miss GET read batch

`internal/db/get_invoice_batch.go::GetInvoiceWithItems` issues ONE
`pool.SendBatch` carrying `GetInvoice` (Q1) + `GetInvoiceItems` (Q2), no
transaction (spec/db-access.md: two statements in one round-trip; spec/cache.md
miss path). A missing header returns `ErrInvoiceNotFound` -> 404, and nothing is
cached (no negative caching). The `sqlGetInvoice` / `sqlGetInvoiceItems` constants
are also asserted byte-for-byte against the spec by the SQL-parity test.

## JSON property casing: snake_case

`spec/openapi.yaml` states "JSON property names: snake_case exactly as written
here, both sides", and every schema uses snake_case (`customer_id`,
`total_minor`, `unit_price_minor`, `created_at`, `page_size`,
`category_surcharge_minor`, ...). All wire DTO json tags in
`internal/api/dto.go` are snake_case. The .NET side uses
`JsonKnownNamingPolicy.SnakeCaseLower` to match.

## access-log event name `http_access`

spec/logging.md line: `msg="http_access"`. The access middleware emits
`msg="http_access"`.

## 400 validation errors-map KEY FORMAT

The errors-map keys are field PATHS. The .NET source-generated `AddValidation()`
emission is canonical; Go emits the same keys (see
services/dotnet/NOTES-equivalence.md):

- Top-level request fields: PascalCase CLR-style names: `CustomerId`,
  `Currency`, `Status`, `Items`.
- Item-level fields: `Items[N].Field` with a ZERO-BASED index and a dot before
  the sub-field, e.g. `Items[0].Sku`, `Items[2].UnitPriceMinor`.
- Path id: key `id` (message `must be a positive integer`).
- Query page: key `page` (messages `must be >= 1` / `must be <= 100000`).
- Unreadable/oversized/malformed body: key `body` with message `is required`.

Each value is an array of frozen `x-validation-messages` strings.

Required-vs-present: Go cannot natively distinguish an absent JSON key from a
present-but-zero value, so `internal/api/decode.go::decodeWithPresence` captures
the set of top-level keys actually present (first-pass raw-map decode). Absent
required top-level field -> `is required`; present-but-invalid -> the specific
frozen message. (Item-level presence is not tracked individually; each present
item is fully validated.)

## validation ordering (400 before 404)

All endpoints validate syntactic/range rules and write the 400 BEFORE any
existence probe (CustomerExists / InvoiceExists / list empty-page probe), per
spec/openapi.yaml "Validation ordering: 400 BEFORE 404".

## page parse

`?page=` non-integer is reported as `must be >= 1` (page_min); a non-integer
fails the minimum constraint path; below 1 -> page_min; above 100000 ->
page_max; empty -> default 1. The .NET side matches the non-integer message.

## Timestamps

`internal/api/timefmt.go` uses layout `2006-01-02T15:04:05.000000Z07:00`
(`.000000` zeros, not `.999999`/RFC3339Nano which TRIM). Always UTC -> literal
`Z`, exactly 6 fractional digits, padded. Same format reused for log `ts`
(`internal/logx/logger.go`) and the `/runtime-stats` `ts`. Asserted by
`internal/api/timefmt_test.go` including the whole-second trailing-zero case.

## Logging

- Async sink `internal/logx/async.go`: bounded channel cap = `LOG_QUEUE_LEN`
  (default 8192), copy-then-blocking-send (never select/default), single drain
  goroutine, `Close` drains then waits (lossless on shutdown), post-close
  synchronous fallback. A `sync.RWMutex` lets many producers send concurrently
  (RLock around the send) while `Close` (Lock) guarantees no send is in flight
  when it closes the channel: no send-on-closed panic.
- slog `JSONHandler` with `ReplaceAttr` rewrites `time`->`ts` (6-digit-Z) and
  lowercases `level`. Frozen line schema: `ts`, `level`, `msg`, `request_id` +
  per-event fields.
- Domain events: `invoice_created` (customer_id, invoice_id, item_count,
  total_minor), `cache_invalidated` (invoice_id), `pdf_stub_written`
  (invoice_id, bytes_written), `quote_computed` (item_count, total_minor).
- Errors: `request_error` (status, error_class) + `exception` (detail) for
  internals. `error_class` is `validation` (malformed/undeserializable/oversized
  request body only) or `internal`. Lifecycle: one `config` line at startup,
  `shutdown_complete` last.
- Access log excluded for `/healthz`, `/readyz`, `/runtime-stats`. NOTE:
  `/healthz` IS counted in `endpoint_calls` (spec/runtime-stats.md lists
  `GET /healthz`) even though it is not access-logged: counting happens via the
  metrics sampler inside the handler, independent of the access middleware.
- Test `internal/logx/async_test.go::TestConcurrentInfoExactlyNLines`: 5000
  concurrent Info calls -> exactly 5000 lines, each unique seq, valid JSON.
  Plus block-on-full-lossless (queue cap 1) and post-close fallback tests.

## pdf-stub

`internal/api/pdfstub.go`: frozen algorithm: lines
`"%PDFSTUB invoice=<id> line=<k>\n"` appended until len >= 32768, then sliced to
exactly 32768. Buffered write (`bufio.Writer` + `Flush`, no fsync) to
`PDF_DIR/{id}.pdfstub` (PDF_DIR default `/data/pdf`, created at startup).
`bytes_written` is always 32768. Response `path` is `PDF_DIR + "/" + id +
".pdfstub"` (forward-slash, equivalence-compared). Asserted by
`pdfstub_test.go` (exact size, prefix, per-line format).

## /runtime-stats

`internal/metrics`: shape per spec/runtime-stats.md. GC from the exact
non-deprecated runtime/metrics names: `/sched/pauses/total/gc:seconds`,
`/gc/cycles/total:gc-cycles` (-> gen2; gen0=gen1=0, Go single generation),
`/gc/heap/allocs:bytes`, `/gc/heap/goal:bytes`,
`/memory/classes/heap/objects:bytes`, `/memory/classes/total:bytes` minus
`/memory/classes/heap/released:bytes` (heap_committed). db_pool from
`pgxpool.Stat()`, redis_pool from go-redis `PoolStats()`. `endpoint_calls` are
plain atomic counters keyed by the frozen route templates. config block echoes
the env knobs; `max_auto_prepare` and `gc_dynamic_adaptation_mode` are null on Go
(.NET-only), `gogc`/`gomemlimit` are strings. `GOMAXPROCS` is pinned to the SUT
core count and reported here.

## Config / shutdown / server timeouts

`internal/config`: env with locked defaults (DB_POOL_MAX 24, DB_POOL_MIN=max,
DB_CONN_LIFETIME 3600s, REDIS_POOL_MAX 16, CACHE_TTL_SECONDS 300, LOG_QUEUE_LEN
8192, BODY_MAX_BYTES 65536, PORT 8080, PG_*/REDIS_ADDR, PDF_DIR /data/pdf). One
`msg="config"` startup line with every effective value + GOMAXPROCS +
processor_count + the pinned http.Server timeouts (ReadHeaderTimeout 5s,
ReadTimeout 30s, WriteTimeout 30s, IdleTimeout 120s). Graceful shutdown:
`srv.Shutdown` -> Redis Close -> pool.Close -> `shutdown_complete` line -> async
writer Close (drain). pgx exec mode left at the DEFAULT
(QueryExecModeCacheStatement); pgx never de-tuned.

## Status

- `gofmt -l .`, `go vet ./...`, `go build ./...`, and `go test ./...` pass
  (pricing goldens A/B/C + tier/threshold ladders, bps_apply, timestamp format,
  SQL-parity, async writer N-lines + lossless + post-close, pdf-stub
  size/prefix/format, validation messages + frozen-string pin).
- The async-writer concurrency is correct by construction (an RWMutex separates
  senders from the closer). CI runs plain `go test ./...`, not `-race`: the race
  detector needs CGO, and the services build with `CGO_ENABLED=0`.
- The image is a `golang:*-bookworm` build into distroless static: `CGO_ENABLED=0`,
  `-trimpath`, `-ldflags '-w -s'`. Both base tags are digest-pinned in the
  Dockerfile.
