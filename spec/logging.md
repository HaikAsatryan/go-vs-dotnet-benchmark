# spec/logging.md — logging contract (both services, frozen)

JSON lines to stdout; docker json-file driver with identical compose config on
both services. Field ORDER inside a line is irrelevant; field NAMES and VALUES
are frozen.

## Common fields on every line

| field | type | value |
|---|---|---|
| ts | string | RFC3339 UTC, exactly 6 fractional digits, Z (same format as wire timestamps) |
| level | string | "info" \| "warn" \| "error" |
| msg | string | event name (below) |
| request_id | string | the request's id (UUIDv4 or inbound X-Request-Id); empty for lifecycle lines |

## Access line — exactly one per HTTP request

`msg="http_access"`, level `info`, additional fields:

| field | type | pin |
|---|---|---|
| method | string | uppercase |
| path | string | the MATCHED ROUTE TEMPLATE, e.g. "/invoices/{id}", never the raw URL (identical cardinality both sides) |
| status | int | response status |
| duration_ms | number | float ms, monotonic clock, measured handler-entry -> response-write-complete (same boundary both sides) |

Excluded from access logging on BOTH sides: `/healthz`, `/readyz`,
`/runtime-stats` (health is the HTTP floor; runtime-stats is bench
instrumentation). Everything else logs exactly one line.

## Domain events (level info, on successful writes)

| msg | fields |
|---|---|
| invoice_created | customer_id, invoice_id, item_count, total_minor |
| cache_invalidated | invoice_id |
| pdf_stub_written | invoice_id, bytes_written |
| quote_computed | item_count, total_minor |

Errors: `msg="request_error"`, level `error`, fields `status`,
`error_class` (validation | internal). `validation` fires on exactly two paths
(2026-08-13 revision): the undeserializable/malformed request-body path — a
non-object/invalid-JSON or unreadable body, which becomes the frozen 400
`{"body":["is required"]}` — and the non-JSON Content-Type path, which becomes
the frozen 415 (spec/openapi.yaml x-error-strings.unsupported_media_type).
Identical on both sides. An OVERSIZED body is the frozen 413 with NO
request_error on either side (the access line's `status` records it; .NET's
Kestrel enforces the limit below the point where the app can log a rejection
event, so both sides pin the access-line-only shape). Field-level validation
400s and all 404s produce NO request_error (recorded via the access line's
`status`). Internal errors additionally log `msg="exception"` (level error,
field `detail`); never inside the access line.

Lifecycle: `msg="config"` once at startup (the effective-config line the runner
captures into the manifest); `msg="shutdown_complete"` last line before exit.

No other lines during measured traffic. No sampling. Level floor `info`, debug
disabled in benchmark builds. Identical levels both sides.

## Async sink contract (frozen)

- Bounded queue, capacity 8192 (`LOG_QUEUE_LEN`).
- BLOCK-ON-FULL, lossless: the producer blocks when the queue is full. Never drop.
  - .NET: console logger `QueueFullMode = Wait`, `MaxQueueLength = 8192`
    (the DEFAULT DROPS; the configured values are asserted in the manifest via
    the config line).
  - Go: slog JSONHandler writing to the published async writer: bounded channel
    cap 8192, plain blocking send (never select/default, which would drop),
    single drain goroutine to stdout.
- Flush-on-shutdown: on SIGTERM stop accepting requests, drain the queue fully,
  then exit. Both sides.
- slog formats inline on the calling goroutine; MEL formats on the caller too:
  the queued unit is a formatted line both sides.

## What the validation suite asserts

The two bench-tier checks this section once pre-registered — the isolated sink
microbench (lines/sec + allocs/op per sink) and the no-drop/backpressure test
(exactly-N lines under a throttled consumer) — were descoped on 2026-08-13
without ever running, and no published number is gated on them (see
docs/METHODOLOGY.md). What remains asserted: the sink configuration (queue
length 8192, block-on-full/Wait) is echoed by each service's config line and
recorded in the manifest, and the bounded lossless contract above stays the
implemented behavior on both sides.
