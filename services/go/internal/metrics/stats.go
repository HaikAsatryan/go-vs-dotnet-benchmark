package metrics

import (
	"runtime"
	"time"

	"ledgerline/server/internal/config"
)

// The runtime-stats JSON shape (spec/runtime-stats.md). Identical keys to the
// .NET side; values are Go-specific. config keys not applicable to Go are still
// present, with null for the .NET-only ones (max_auto_prepare,
// gc_dynamic_adaptation_mode).

type BuildBlock struct {
	Language       string `json:"language"`
	RuntimeVersion string `json:"runtime_version"`
	ServiceVersion string `json:"service_version"`
	DataLayer      string `json:"data_layer"`
	ProcessorCount int    `json:"processor_count"`
}

type ConfigBlock struct {
	DBPoolMax             int     `json:"db_pool_max"`
	DBPoolMin             int     `json:"db_pool_min"`
	DBConnLifetimeSeconds int     `json:"db_conn_lifetime_seconds"`
	RedisPoolMax          int     `json:"redis_pool_max"`
	CacheTTLSeconds       int     `json:"cache_ttl_seconds"`
	LogQueueLen           int     `json:"log_queue_len"`
	MaxAutoPrepare        *int    `json:"max_auto_prepare"`           // null on Go
	GOGC                  *string `json:"gogc"`                       // string per spec
	GOMEMLIMIT            *string `json:"gomemlimit"`                 // string per spec
	GCDynamicAdaptionMode *string `json:"gc_dynamic_adaptation_mode"` // null on Go
}

type GCCollections struct {
	Gen0 int64 `json:"gen0"`
	Gen1 int64 `json:"gen1"`
	Gen2 int64 `json:"gen2"`
}

type GCBlock struct {
	Collections         GCCollections `json:"collections"`
	PauseTotalSeconds   float64       `json:"pause_total_seconds"`
	HeapInUseBytes      int64         `json:"heap_in_use_bytes"`
	HeapCommittedBytes  int64         `json:"heap_committed_bytes"`
	HeapGoalBytes       int64         `json:"heap_goal_bytes"`
	AllocatedTotalBytes int64         `json:"allocated_total_bytes"`
}

type DBPoolBlock struct {
	Max                     int     `json:"max"`
	Total                   int     `json:"total"`
	Idle                    int     `json:"idle"`
	InUse                   int     `json:"in_use"`
	AcquireCount            int64   `json:"acquire_count"`
	EmptyAcquireCount       int64   `json:"empty_acquire_count"`
	AcquireWaitTotalSeconds float64 `json:"acquire_wait_total_seconds"`
}

type RedisPoolBlock struct {
	Max   int `json:"max"`
	Total int `json:"total"`
	Idle  int `json:"idle"`
	InUse int `json:"in_use"`
}

type Stats struct {
	TS                     string           `json:"ts"`
	UptimeSeconds          float64          `json:"uptime_seconds"`
	Build                  BuildBlock       `json:"build"`
	Config                 ConfigBlock      `json:"config"`
	GC                     GCBlock          `json:"gc"`
	DBPool                 DBPoolBlock      `json:"db_pool"`
	RedisPool              RedisPoolBlock   `json:"redis_pool"`
	GoroutinesOrThreadpool int              `json:"goroutines_or_threadpool"`
	EndpointCalls          map[string]int64 `json:"endpoint_calls"`
}

// tsLayout: frozen 6-digit Z timestamp (spec/runtime-stats.md ts example).
const tsLayout = "2006-01-02T15:04:05.000000Z07:00"

// Build produces the full Stats snapshot. cfg supplies the echoed config block;
// serviceVersion/dataLayer/runtimeVersion are passed by the caller (main wiring).
func (s *Sampler) Build(cfg config.Config, runtimeVersion, serviceVersion, dataLayer string, gogc, gomemlimit string) Stats {
	gc := s.readGC()
	now := time.Now()

	st := s.pool.Stat()
	rs := s.redis.PoolStats()

	calls := make(map[string]int64, len(endpointKeys))
	for _, k := range endpointKeys {
		calls[k] = s.counters[k].Load()
	}

	return Stats{
		TS:            now.UTC().Format(tsLayout),
		UptimeSeconds: now.Sub(s.start).Seconds(),
		Build: BuildBlock{
			Language:       "go",
			RuntimeVersion: runtimeVersion,
			ServiceVersion: serviceVersion,
			DataLayer:      dataLayer,
			ProcessorCount: runtime.NumCPU(),
		},
		Config: ConfigBlock{
			DBPoolMax:             int(cfg.DBPoolMax),
			DBPoolMin:             int(cfg.DBPoolMin),
			DBConnLifetimeSeconds: int(cfg.DBConnLifetime.Seconds()),
			RedisPoolMax:          cfg.RedisPoolMax,
			CacheTTLSeconds:       cfg.CacheTTLSeconds,
			LogQueueLen:           cfg.LogQueueLen,
			MaxAutoPrepare:        nil, // Go: pgx caches by default; no analog
			GOGC:                  &gogc,
			GOMEMLIMIT:            &gomemlimit,
			GCDynamicAdaptionMode: nil, // .NET-only
		},
		GC: GCBlock{
			// Go has a single generation; cycles map into gen2, gen0=gen1=0
			// (documented, spec/runtime-stats.md).
			Collections:         GCCollections{Gen0: 0, Gen1: 0, Gen2: gc.cycles},
			PauseTotalSeconds:   gc.pauseTotal,
			HeapInUseBytes:      gc.heapInUse,
			HeapCommittedBytes:  gc.heapCommitted,
			HeapGoalBytes:       gc.heapGoal,
			AllocatedTotalBytes: gc.allocated,
		},
		DBPool: DBPoolBlock{
			Max:                     int(st.MaxConns()),
			Total:                   int(st.TotalConns()),
			Idle:                    int(st.IdleConns()),
			InUse:                   int(st.AcquiredConns()),
			AcquireCount:            st.AcquireCount(),
			EmptyAcquireCount:       st.EmptyAcquireCount(),
			AcquireWaitTotalSeconds: st.AcquireDuration().Seconds(),
		},
		RedisPool: RedisPoolBlock{
			Max:   cfg.RedisPoolMax,
			Total: int(rs.TotalConns),
			Idle:  int(rs.IdleConns),
			InUse: int(rs.TotalConns - rs.IdleConns),
		},
		GoroutinesOrThreadpool: runtime.NumGoroutine(),
		EndpointCalls:          calls,
	}
}
