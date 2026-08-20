# services/dotnet: implementation notes & decisions

The .NET SUT for Ledgerline. spec/ is the single source of truth; decisions below resolve
ambiguities toward the LOWEST cross-language divergence risk.

## Build shape

- `dotnet publish -c Release` produces `Ledgerline.dll`; the test project and xunit are
  excluded from the publish output by the csproj's `Compile Remove`.
- The EF compiled model is committed under `Data/CompiledModels`, auto-discovered via the
  `[DbContextModel]` assembly attribute and wired explicitly with `.UseModel` (see
  "Compiled model").

## Builder choice

`WebApplication.CreateSlimBuilder`, with the full validation source-gen + ProblemDetails
pipeline. `UseAdminDatabase` is not used.

## JSON casing: snake_case, NOT camelCase

spec/openapi.yaml line 11 freezes snake_case property names ("JSON property names: snake_case
exactly as written here, both sides"). Every wire DTO
carries explicit `[JsonPropertyName("snake_case")]`; the STJ source-gen context sets
`PropertyNamingPolicy = Unspecified` so the explicit names are authoritative. Verified on the wire:
the /pricing/quote 200 body emits `subtotal_minor`, `category_surcharge_minor`, etc.

## Validation message braces

DataAnnotations runs `string.Format(ErrorMessage, displayName)` on the message. The frozen messages
`must match ^[A-Z]{3}$` and `must match ^[A-Z]{2}-[0-9]{4}$` contain `{N}` sequences that
string.Format treats as positional placeholders → it threw an "Index (zero based)…" message instead
of the frozen text. Fix: brace-double in the C# attribute (`^[A-Z]{{3}}$`) so the EMITTED wire text
is the literal frozen `^[A-Z]{3}$`. Confirmed by WebApplicationFactory capture. The Go side must
emit the un-doubled literal (no string.Format), so its source is the bare frozen string.

## Id path-param parsing (400 vs 404)

spec: "Non-positive or non-integer → 400; well-formed but absent → 404." A route constraint
`{id:long}` would 404 on non-integer (route miss), violating the spec. So `{id}` is bound as a
string and parsed manually (`InvoiceEndpoints.TryParseId`): parse-fail OR `< 1` → 400 validation
problem with errors map `{"id":["must be a positive integer"]}`; valid → proceed (404 if absent).
This is the lowest-divergence choice (Go's ServeMux `{id}` wildcard parses identically).

## 400-before-404 ordering

For POST /invoices the body is validated by AddValidation (400) before the handler runs the customer
existence probe (404). For GET/pdf the id is parsed (400) before the DB/cache lookup (404). Ordering
holds structurally.

## Create-path round-trip ledger (EF + raw ADO.NET)

- EF (headline efcore): Q7 existence via AnyAsync (EXISTS, same shape as Go/ADO) → build
  Invoice+Items graph → explicit tx (DB-default READ COMMITTED, auto-savepoints off) → ONE
  SaveChangesAsync (header RETURNING id+created_at + N items batched) → Q6 balance via
  ExecuteUpdateAsync (ATOMIC increment) → commit. A later revision replaced the original
  tracked read-modify-write balance: it wrote an absolute value and lost concurrent updates on
  hot customers (Go/ADO increment in place; the seeder invariant only survives the increment
  form). Cost disclosed in spec/db-access.md: EF cannot fold Q6 into the item batch, so the EF
  create carries one extra round-trip vs pgx/NpgsqlBatch.
- Raw ADO.NET variant: Q7 existence, then explicit READ COMMITTED tx: Q4 header (RETURNING id), then a
  second NpgsqlBatch for Q5×N items + Q6 balance, then commit. Items need the generated invoice id
  for their FK, which a single PG batch cannot supply to earlier commands, so header and item-batch
  are separate statements within ONE transaction.
- TRUE wire round-trip count may be 4 (BEGIN + existence + write-batch + commit) for raw ADO.NET, and
  similarly for EF if BEGIN is counted separately. Per the spec round-trip note this is EXPECTED;
  the code is NOT contorted to force a literal 3.

## Compiled model

Generated with `dotnet ef dbcontext optimize` (dotnet-ef 10.0.11 via committed dotnet-tools.json),
output committed to Data/CompiledModels. EF 10 auto-discovers it via the generated
`[assembly: DbContextModel(...)]` attribute; `CompiledModelWiring.Apply` ALSO calls
`.UseModel(LedgerDbContextModel.Instance)` explicitly (deterministic). A
`DesignTimeDbContextFactory` lets the optimize command build the model with no live DB/env.
The compiled model is a non-default EF optimisation with no Go analogue, so it is declared
as an asymmetry in docs/FAIRNESS.md rather than treated as a default.
NOTE: the compiled (runtime) model does not expose `IIndex.IsDescending`; the descending-on-id of
the list index is instead proven by the generated SQL (`ORDER BY i.id DESC` in EfQueryShapeTests).

## Logging

Stock JsonConsoleFormatter nests under State/Message and emits a 7-digit timestamp, so it cannot
produce the frozen schema. Custom `LedgerConsoleFormatter` (registered via
`AddConsole(o => o.FormatterName=...)` + `AddConsoleFormatter`) writes the exact flat JSON:
`{ts(6-frac UTC Z), level(lowercase), msg(event name), request_id, ...flat event fields...}`.
`{OriginalFormat}` is filtered out. ConsoleLoggerOptions set QueueFullMode=Wait + MaxQueueLength
from LOG_QUEUE_LEN (8192). [LoggerMessage] source-gen drives http_access + domain events; the
constant Message string is the event name (msg). SYSLIB1015 (args not in message template) is
expected and suppressed; the args are intentionally structured fields, not interpolated.

## Access log

Middleware logs the MATCHED ROUTE TEMPLATE (`/invoices/{id}`), not the raw URL, via
`RouteEndpoint.RoutePattern.RawText`. /healthz, /readyz, /runtime-stats excluded. Exactly one line
per request; duration measured handler-entry → response-write-complete with Stopwatch (monotonic).

## /runtime-stats

`RuntimeStatsCollector` singleton owns a MeterListener over the `System.Runtime` and `Npgsql`
meters (instrument names per the spec mapping table: dotnet.gc.collections{gc.heap.generation},
dotnet.gc.pause.time, dotnet.gc.last_collection.heap.size,
dotnet.gc.last_collection.memory.committed_size, dotnet.gc.heap.total_allocated,
dotnet.thread_pool.thread.count, db.client.connection.max, db.client.connection.count{state}).
Endpoint counters are atomic increments on the frozen route-template keys. Config echo block + nulls
where .NET has no analog (gogc, gomemlimit, gc.heap_goal_bytes, and the Npgsql-unexposed
acquire_count / empty_acquire_count / acquire_wait_total_seconds). Shape verified by tests; values
are runtime-specific and only meaningful against live infra.

## Redis / cache

StackExchange.Redis single multiplexer (idiomatic; the documented asymmetry vs go-redis pool).
Serialize-then-cache: a miss serializes the InvoiceDto via STJ source-gen, SETs the bytes with TTL,
returns those same bytes; a hit returns the cached bytes verbatim via `Results.Bytes(..,
"application/json")`. No negative caching. DEL on create.

## NpgsqlDataSource registration

`AddNpgsqlDataSource` lives in the separate Npgsql.DependencyInjection package; to keep the
dependency surface to the pinned set, the data source is registered as a singleton built from
`NpgsqlDataSourceBuilder` directly. Same pool/connection-string as EF.

## Package versions (pins)

Npgsql.EntityFrameworkCore.PostgreSQL 10.0.3, Npgsql 10.0.3, StackExchange.Redis 3.1.13,
Microsoft.EntityFrameworkCore 10.0.11 (explicit runtime pin), Microsoft.EntityFrameworkCore.Design
10.0.11 (PrivateAssets=all).
Tests: xunit 2.9.3, xunit.runner.visualstudio 3.1.5, Microsoft.NET.Test.Sdk 18.8.1,
Microsoft.AspNetCore.Mvc.Testing 10.0.11. dotnet-ef tool 10.0.11.
Dapper 2.1.79 (variant-only). v1 removed Dapper on the grounds that its named-parameter
model cannot keep the statement bodies byte-identical; a later revision reinstated
it as a THIRD data layer (DATA_LAYER=dapper) after establishing that Npgsql's client-side
rewriting produces the identical positional wire text (DapperSqlParityTests pins the
substitution). The three-layer ladder: efcore (full ORM) / dapper (micro-ORM) /
ado (floor): lets the report price each abstraction step against Go's sqlc.

## Notes

- InvoiceItem `id` in the POST 201 body: all three data layers return the real DB identity ids.
  spec/db-access.sql `InsertInvoiceItem` does `RETURNING id`; EF gets the ids from SaveChanges,
  the raw ADO.NET NpgsqlBatch and the Go SendBatch read the returned ids. See NOTES-equivalence.md
  for the full create-response equivalence contract.
- `char(3)` currency comes back space-padded from PG; reads `TrimEnd()` it so the wire value is the
  3-char code. Equivalence-safe.
