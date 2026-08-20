# spec/db-access.md — DB access policy (both services, frozen)

The canonical SQL lives in `spec/db-access.sql` (sqlc consumes it verbatim via
its annotations; the .NET raw ADO.NET variant uses the same statement bodies
literally; EF LINQ must produce the same SQL shape, asserted by capturing
EF-generated SQL in tests). This file is the human contract around it.

Two pinned EF shape deviations (disclosed 2026-08-14, asserted by
EfQueryShapeTests): EF 10 parameterizes the row-limiting operator, so Q3 ships
`LIMIT $2 OFFSET $3` (three parameters) where sqlc/ado/dapper send the literal
`LIMIT 20 OFFSET $2` — `EF.Constant` cannot force a literal inside `Take(int)`,
so the parameterized form IS the idiomatic EF shape; and EF's Q1 carries a
trailing `LIMIT 1` (`FirstOrDefault` idiom) the other layers do not. Both are
plan-equivalent (same index paths); the statement text and parameter counts
differ, which shows up in pg_stat views and prepared-statement signatures.

Two .NET variants exist beside the EF Core headline (2026-08-13 revision):

- **ado** — hand-written ADO.NET over Npgsql with positional `$1` parameters,
  statement bodies byte-identical to this file. The no-ORM floor.
- **dapper** — idiomatic Dapper with named parameters; Npgsql client-side
  rewrites each `@name` to the identical positional text before it hits the
  wire (asserted by a substitution test). The micro-ORM middle point. Its
  per-command rewriting cost is part of what Dapper costs in production and is
  not tuned away. Dapper has no batch API, so the create path's item batch
  drops to NpgsqlBatch exactly like the ado variant — the wire ledger is held
  constant across all layers so the contrast isolates mapping cost.

## Queries (bodies in db-access.sql)

| id | name | used by | notes |
|---|---|---|---|
| Q1 | GetInvoice | GET /invoices/{id} (cache miss) | by PK |
| Q2 | GetInvoiceItems | GET /invoices/{id} (cache miss) | WHERE invoice_id ORDER BY id |
| Q3 | ListInvoicesByCustomer | GET /customers/{id}/invoices | ORDER BY id DESC LIMIT 20 OFFSET $2; must hit idx_invoices_customer_id_id_desc with no sort node |
| Q3b | CustomerExists | list endpoint, ONLY when the page is empty | happy path stays one query; both sides identical |
| Q4 | InsertInvoice | POST /invoices | RETURNING id, created_at (both DB-generated; needed for the 201 body, no extra query) |
| Q5 | InsertInvoiceItem | POST /invoices | xN (3-8) |
| Q6 | UpdateCustomerBalance | POST /invoices | balance_minor = balance_minor + $2 |
| Q7 | CustomerExists | POST /invoices, before the write batch | same statement as Q3b |

GET /invoices/{id} on a cache miss issues Q1 then Q2. Pin: issued as two
statements in ONE round-trip where the driver supports it idiomatically
(pgx: one batch; EF: two queries in one connection acquisition; the round-trip
delta, if any, is documented in services/dotnet/NOTES-equivalence.md — the
DB-CPU-per-request column that would have priced it was descoped 2026-08-13).

POST /invoices/{id}/pdf-stub probes invoice existence with the Q1 header lookup
ONLY — one statement, items never loaded — identically on every data layer.

Absent-id note (unexercised by the workload; the GET keyspace is seeded-only):
the batched implementations (pgx SendBatch, ADO NpgsqlBatch) pre-queue Q1+Q2, so
a 404 still executes Q2 server-side; EF issues the queries sequentially and skips
Q2 when the header is absent. Disclosed, not contorted away.

## POST /invoices transaction (the fairness-critical mirror)

One transaction, READ COMMITTED (PG default; pinned; neither side elevates).
Order inside: Q7 existence -> Q4 header (RETURNING id, created_at) ->
Q5 xN items (RETURNING id) -> Q6 balance.

Q6 is an ATOMIC increment (`balance_minor = balance_minor + $2`) on every data
layer. A tracked read-modify-write (load balance, add, write the absolute value)
is NOT equivalent: under the workload's Zipfian hot-customer contention it loses
concurrent updates and breaks the seeder invariant
`balance_minor == sum(total_minor)`. (v1 pinned the EF tracked-entity form; the
2026-08-13 revision replaced it with `ExecuteUpdate` for exactly this reason.)

The header id is IDENTITY-generated, and the N item inserts bind it as a
parameter, so NO driver can put header + items in one pipelined batch (the
RETURNING value cannot propagate across statements inside a batch). The honest
shared structure, identical on all three data layers and ASSERTED EQUAL at
integration (round-trip counts measured via PG instrumentation on the Linux
box):

```
existence/load (1) + BEGIN + header insert RETURNING (1)
  + ONE batched round-trip [Q5 xN + Q6] + COMMIT
```

- .NET / EF Core: Q7 via `AnyAsync` (the same EXISTS statement shape as Go and
  raw ADO.NET); explicit `BeginTransactionAsync` (DB-default READ COMMITTED);
  ONE `SaveChangesAsync` for the invoice graph (EF inserts the principal first,
  reads the IDENTITY id back via RETURNING, then batches the dependent item
  inserts); then Q6 as `ExecuteUpdateAsync` (atomic increment); commit.
  Automatic savepoints are disabled (pgx issues none). DISCLOSED asymmetry: EF
  cannot fold the Q6 increment into the item batch the way pgx's SendBatch and
  NpgsqlBatch do, so the EF create carries one extra round-trip. The DB-side
  statement set is identical.
- Go / pgx + sqlc: `pool.Begin`; Q4 via `tx.QueryRow` (RETURNING id,
  created_at); Q5 xN + Q6 queued on one `pgx.Batch` -> `tx.SendBatch` (single
  pipelined round-trip; item ids read from the batch results); `tx.Commit`.
- Raw ADO.NET variant: same SQL strings; Q4 command, then one NpgsqlBatch
  [Q5 xN + Q6] inside an explicit READ COMMITTED transaction.

All three return the REAL DB-generated item ids in the 201 body (pinned at
integration 2026-06-06; EF gets them from SaveChanges, pgx/Npgsql from the
per-item RETURNING id).

After commit (outside the tx): Redis `DEL invoice:{new_id}` (spec/cache.md);
then the `invoice_created` + `cache_invalidated` log events.

Hot-row note: Zipfian customer selection makes Q6 contend on hot customer rows.
Realistic, identical for both languages; lock waits visible in pg_stat and
reported.

## Exec / prepare modes per variant

| stack | headline | "prepared parity" variant |
|---|---|---|
| Go pgx | QueryExecModeCacheStatement (pgx DEFAULT: auto server-side prepared cache) | unchanged (pgx is never de-tuned) |
| .NET Npgsql | Max Auto Prepare=0 (Npgsql DEFAULT, off) | Max Auto Prepare=20, Auto Prepare Min Usages=5 |

The asymmetry of the two documented defaults is the #1 PG fairness lever:
disclosed prominently; quantified by the variant. Both drivers use the extended
query protocol with binary parameter/result formats by default (asserted).
Npgsql multiplexing stays OFF (experimental, no pgx analog).

## Pools (single env surface, both services)

| env | default | Go | .NET |
|---|---|---|---|
| DB_POOL_MAX | 24 | pgxpool MaxConns | Npgsql Maximum Pool Size |
| DB_POOL_MIN | = DB_POOL_MAX | pgxpool MinConns | Npgsql Minimum Pool Size |
| DB_CONN_LIFETIME | 3600 (s) | MaxConnLifetime | Connection Lifetime (explicit pin; doc-default ambiguity resolved by always setting it) |
| REDIS_POOL_MAX | 16 | go-redis PoolSize | SE.Redis (multiplexer; documented asymmetry, spec/cache.md) |

min = max so neither pool cold-starts or idle-prunes connections mid-run.
DB_POOL_MAX < PG max_connections (100). The pinned values are echoed in the
manifest, identical across both services. (The once-planned connection sweep
that would have selected the value empirically was descoped 2026-08-13 without
running; the pin is a declared choice, not a swept optimum.)

Pool warmup: during warmup the runner drives enough concurrency to open all
connections; the warmup gate requires `/runtime-stats` db_pool.total == max.

## Seeder contract (consumed by seed/)

- One canonical deterministic generator (seed 42), exact counts per scale
  (spec/workload.yaml `seeding`), money int64, no floats.
- COPY order: customers -> invoices -> invoice_items, ids explicit and dense,
  rows emitted in PK order. Binary COPY (pgx CopyFrom).
- After COPY: restart both IDENTITY sequences past the seeded max
  (see spec/schema.sql header).
- customers.created_at / invoices.created_at: deterministic timestamps derived
  from the seed stream (any fixed epoch progression; equivalence never compares
  seeded timestamps across DB loads, only across services on the SAME load).
- Post-load invariants (all must hold, non-zero exit otherwise):
  - exact row counts per scale;
  - per customer: balance_minor == COALESCE(sum(invoices.total_minor), 0);
  - per invoice: total_minor == sum(items.line_total_minor);
  - per item: line_total_minor == qty * unit_price_minor;
  - idx_invoices_customer_id_id_desc exists (pg_indexes).

## Error reporting

Error rate is always reported alongside latency (headline valid only when
errors < 0.1%, spec/slo.yaml). DB errors map to 500/internal (RFC 7807);
statement_timeout (5 s) and lock_timeout (1 s) come from the PG config and
surface as 500s, counted as errors.
