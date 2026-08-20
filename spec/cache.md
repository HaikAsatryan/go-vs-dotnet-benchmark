# spec/cache.md — cache-aside contract (both services, frozen)

```yaml
policy: cache-aside
backend: Redis 8.x (AGPL noted in README; Valkey documented drop-in)
key: "invoice:{id}"            # base-10 invoice id, no padding
value: >
  The canonical GET /invoices/{id} JSON body: the exact UTF-8 bytes the endpoint
  returns. SERIALIZE-THEN-CACHE pin: both services serialize the response first
  and store those bytes, so a hit serves byte-equivalent output to a miss and
  the cache layer cannot introduce a hit/miss output difference.
ttl_seconds: 300               # env CACHE_TTL_SECONDS (default 300); capacity-gate lever raises it
```

## Read path (GET /invoices/{id})

1. `GET invoice:{id}`
2. hit: return the cached bytes (no DB, no re-serialization).
3. miss: Q1+Q2 from PG; absent -> 404 (NOT cached: no negative caching);
   present -> build canonical body, `SET invoice:{id} <body> EX <ttl>`, return body.

## Write path (POST /invoices)

After commit: `DEL invoice:{new_id}`. Guards stale/negative entries; one extra
Redis RTT in the create path, identical both sides. (With no negative caching
and v1's seeded-only GET keyspace the DEL is a near-no-op, but it is the
realistic invalidation pattern and its cost belongs in the create path.)

## Server config (infra/compose, not client concerns)

```
maxmemory 1500mb
maxmemory-policy allkeys-lru
save ""            # persistence off
appendonly no
```

## Client pins

| env | default | Go | .NET |
|---|---|---|---|
| REDIS_ADDR | infra | go-redis Addr | SE.Redis endpoint |
| REDIS_POOL_MAX | 16 | go-redis PoolSize | SE.Redis multiplexer (see below) |

Asymmetry (kept on purpose, FAIRNESS): go-redis uses a connection pool;
StackExchange.Redis uses a single multiplexed connection. Both are the
idiomatic production model for their stack. REDIS_POOL_MAX applies literally to
go-redis; on .NET it is recorded but the multiplexer model is documented as the
idiomatic equivalent. Redis-side utilization is reported. SE.Redis client-side
caching/tracking stays OFF (verified).

## Hit rate

Target ~0.80 by construction (Zipf alpha=1.0 over the invoice keyspace + TTL
300 s). Measured and asserted in the run manifest (Redis INFO keyspace_hits /
misses delta over the window). Capacity-gate lever 2 raises the target to 0.90
via CACHE_TTL_SECONDS (and, if needed, hot-set concentration), applied
identically to both languages and recorded.
