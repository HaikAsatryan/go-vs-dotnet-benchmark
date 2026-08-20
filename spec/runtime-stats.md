# spec/runtime-stats.md — GET /runtime-stats contract (both services, frozen)

Identical JSON SHAPE on both services; values are runtime-specific. Polled by
the Python runner at 1 Hz for: warmup gates (GC-steady, pools-warm,
per-endpoint call counts), the per-run GC/pool table, and the effective-config
echo. Excluded from the workload mix and from access logging. The recorded
resource numbers still come from cgroup v2, never from this endpoint
(corroboration only).

## Shape

```json
{
  "ts": "2026-06-06T00:00:00.000000Z",
  "uptime_seconds": 12.3,
  "build": {
    "language": "go|dotnet",
    "runtime_version": "go1.26.x | 10.0.x",
    "service_version": "git sha or dev",
    "data_layer": "sqlc | efcore | ado",
    "processor_count": 6
  },
  "config": {
    "db_pool_max": 24, "db_pool_min": 24, "db_conn_lifetime_seconds": 3600,
    "redis_pool_max": 16, "cache_ttl_seconds": 300, "log_queue_len": 8192,
    "max_auto_prepare": 0, "gogc": "100", "gomemlimit": "9663676416",
    "gc_dynamic_adaptation_mode": "1"
  },
  "gc": {
    "collections": { "gen0": 0, "gen1": 0, "gen2": 0 },
    "pause_total_seconds": 0.0,
    "heap_in_use_bytes": 0,
    "heap_committed_bytes": 0,
    "heap_goal_bytes": 0,
    "allocated_total_bytes": 0
  },
  "db_pool": {
    "max": 24, "total": 0, "idle": 0, "in_use": 0,
    "acquire_count": 0, "empty_acquire_count": 0, "acquire_wait_total_seconds": 0.0
  },
  "redis_pool": { "max": 16, "total": 0, "idle": 0, "in_use": 0 },
  "goroutines_or_threadpool": 0,
  "endpoint_calls": {
    "GET /invoices/{id}": 0,
    "GET /customers/{id}/invoices": 0,
    "POST /pricing/quote": 0,
    "POST /invoices": 0,
    "POST /invoices/{id}/pdf-stub": 0,
    "GET /healthz": 0
  }
}
```

`config` keys not applicable to a runtime are present with `null` (e.g. `gogc`
on .NET). `endpoint_calls` keys are the frozen route templates above; counters
are process-lifetime totals (plain atomic increments) and drive the
per-endpoint warmup gate.

## Field -> source mapping (frozen)

| field | Go | .NET |
|---|---|---|
| gc.collections | `/gc/cycles/total:gc-cycles` total into gen2; gen0=gen1=0 (Go has one generation; documented) | System.Runtime `dotnet.gc.collections` by generation |
| gc.pause_total_seconds | `/sched/pauses/total/gc:seconds` (NOT the deprecated `/gc/pauses:seconds`) | `dotnet.gc.pause.time` cumulative |
| gc.heap_in_use_bytes | `/memory/classes/heap/objects:bytes` | `dotnet.gc.last_collection.heap.size` (nearest analog, documented) |
| gc.heap_committed_bytes | `/memory/classes/total:bytes` minus `/memory/classes/heap/released:bytes` | `dotnet.gc.last_collection.memory.committed_size` |
| gc.heap_goal_bytes | `/gc/heap/goal:bytes` | null (no direct DATAS analog; documented) |
| gc.allocated_total_bytes | `/gc/heap/allocs:bytes` | `dotnet.gc.heap.total_allocated` |
| db_pool.* | pgxpool.Stat(): MaxConns, TotalConns, IdleConns, AcquiredConns, AcquireCount, EmptyAcquireCount, AcquireDuration | Npgsql metrics (`db.client.connection.max`, `db.client.connection.count{state}`) via MeterListener; counters not exposed by Npgsql are null |
| redis_pool.* | go-redis PoolStats() | SE.Redis GetCounters() mapped; multiplexer model documented |
| goroutines_or_threadpool | runtime.NumGoroutine() | `dotnet.thread_pool.thread.count` |

PIN: where a runtime lacks a true analog the field is still present (null or
documented best-effort) so the shape is identical and the runner has one code
path. Warmup GC-steady signals: Go = collections cadence flat; .NET = gen0-2
counts cadence + heap_committed flat.

Equivalence suite checks SHAPE only (keys/types), never values.
